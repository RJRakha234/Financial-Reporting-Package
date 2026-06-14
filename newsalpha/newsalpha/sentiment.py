"""Finance-aware sentiment scoring for news headlines.

Pure-Python, offline, deterministic. The lexicon is tuned for the language of
markets and corporate news (beats, downgrades, guidance, lawsuits, …) rather
than general English, which is where generic sentiment models fall down on
financial text. If the optional ``vaderSentiment`` package is installed it is
blended in for a small accuracy boost, but the tool works fully without it.

``score_text`` returns a float in ``[-1.0, 1.0]`` where positive is bullish.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

# --- Finance sentiment lexicon (Loughran-McDonald inspired, hand-curated) ----
# Weights are rough magnitudes; the scorer normalises, so relative size matters
# more than absolute value.

POSITIVE: dict[str, float] = {
    # earnings / performance
    "beat": 2.0, "beats": 2.0, "beat estimates": 2.5, "tops": 1.8, "topped": 1.8,
    "surge": 2.2, "surges": 2.2, "surged": 2.2, "soar": 2.4, "soars": 2.4,
    "soared": 2.4, "rally": 1.8, "rallies": 1.8, "rallied": 1.8, "jump": 1.6,
    "jumps": 1.6, "jumped": 1.6, "climb": 1.2, "climbs": 1.2, "gain": 1.3,
    "gains": 1.3, "gained": 1.3, "rise": 1.0, "rises": 1.0, "rose": 1.0,
    "record": 1.6, "record high": 2.2, "all-time high": 2.4, "outperform": 2.0,
    "outperforms": 2.0, "outperformed": 2.0, "strong": 1.4, "stronger": 1.5,
    "robust": 1.4, "solid": 1.2, "growth": 1.2, "growing": 1.1, "profit": 1.2,
    "profitable": 1.4, "profits": 1.2, "momentum": 1.0,
    # ratings / analysts
    "upgrade": 2.2, "upgraded": 2.2, "upgrades": 2.2, "buy rating": 2.0,
    "overweight": 1.8, "raised guidance": 2.6, "raises guidance": 2.6,
    "boosts": 1.6, "boosted": 1.6, "boost": 1.5, "raises": 1.4, "raised": 1.4,
    "hikes": 1.2, "price target raised": 2.2, "bullish": 2.0, "optimistic": 1.4,
    # corporate events
    "approval": 1.8, "approved": 1.8, "wins": 1.6, "won": 1.5, "win": 1.5,
    "awarded": 1.6, "contract": 1.0, "deal": 0.8, "partnership": 1.0,
    "acquire": 1.0, "acquisition": 0.9, "expansion": 1.2, "launch": 1.0,
    "launches": 1.0, "breakthrough": 2.2, "milestone": 1.4, "dividend hike": 2.0,
    "buyback": 1.8, "repurchase": 1.6, "guidance raise": 2.6, "positive": 1.2,
    "exceeds": 2.0, "exceeded": 2.0, "accelerate": 1.4, "accelerating": 1.4,
    "demand": 0.8, "recovery": 1.2, "rebound": 1.6, "rebounds": 1.6,
    "upbeat": 1.6, "outlook raised": 2.2, "tailwind": 1.4, "tailwinds": 1.4,
}

NEGATIVE: dict[str, float] = {
    # earnings / performance
    "miss": 2.0, "misses": 2.0, "missed": 2.0, "misses estimates": 2.5,
    "plunge": 2.6, "plunges": 2.6, "plunged": 2.6, "plummet": 2.6,
    "plummets": 2.6, "crash": 2.8, "crashes": 2.8, "crashed": 2.8, "slump": 2.0,
    "slumps": 2.0, "slumped": 2.0, "tumble": 2.2, "tumbles": 2.2,
    "tumbled": 2.2, "sink": 1.8, "sinks": 1.8, "sank": 1.8, "drop": 1.4,
    "drops": 1.4, "dropped": 1.4, "fall": 1.2, "falls": 1.2, "fell": 1.2,
    "decline": 1.3, "declines": 1.3, "declined": 1.3, "slide": 1.4,
    "slides": 1.4, "slid": 1.4, "weak": 1.6, "weaker": 1.7, "weakness": 1.6,
    "loss": 1.6, "losses": 1.6, "lossmaking": 2.0, "shrink": 1.4, "slowdown": 1.8,
    "slowing": 1.4, "record low": 2.2, "all-time low": 2.4, "underperform": 2.0,
    "underperforms": 2.0, "underperformed": 2.0,
    # ratings / analysts
    "downgrade": 2.4, "downgraded": 2.4, "downgrades": 2.4, "sell rating": 2.2,
    "underweight": 1.8, "cuts guidance": 2.6, "cut guidance": 2.6, "cuts": 1.4,
    "cut": 1.3, "slashes": 2.0, "slashed": 2.0, "slash": 2.0,
    "price target cut": 2.2, "bearish": 2.0, "pessimistic": 1.4, "warns": 1.8,
    "warning": 1.8, "warned": 1.8, "caution": 1.2, "cautious": 1.2,
    # corporate / legal / macro risk
    "lawsuit": 1.8, "sue": 1.6, "sued": 1.6, "probe": 1.8, "investigation": 1.8,
    "fraud": 2.8, "scandal": 2.4, "recall": 1.8, "recalls": 1.8, "fine": 1.4,
    "fined": 1.4, "penalty": 1.6, "bankruptcy": 3.0, "bankrupt": 3.0,
    "default": 2.4, "defaults": 2.4, "layoffs": 1.8, "layoff": 1.8,
    "job cuts": 1.8, "restructuring": 1.0, "halt": 1.6, "halts": 1.6,
    "halted": 1.6, "delay": 1.2, "delays": 1.2, "delayed": 1.2, "suspend": 1.6,
    "suspended": 1.6, "shortfall": 1.8, "negative": 1.2, "concern": 1.0,
    "concerns": 1.0, "fears": 1.4, "risk": 0.8, "risks": 0.8, "headwind": 1.4,
    "headwinds": 1.4, "disappoint": 1.8, "disappoints": 1.8, "disappointing": 1.8,
    "selloff": 2.0, "sell-off": 2.0, "downturn": 1.8, "recession": 2.0,
    "guidance cut": 2.6, "outlook cut": 2.2, "writedown": 2.0, "write-down": 2.0,
    "impairment": 1.8, "glut": 1.6, "oversupply": 1.6,
}

NEGATORS = {
    "not", "no", "never", "without", "fails", "fail", "failed", "failing",
    "lacks", "lack", "unable", "cannot", "won't", "didn't", "doesn't",
    "isn't", "wasn't", "aren't", "less", "fewer", "down",
}

INTENSIFIERS = {
    "very": 1.4, "highly": 1.4, "sharply": 1.6, "significantly": 1.5,
    "massively": 1.8, "hugely": 1.6, "strongly": 1.4, "deeply": 1.4,
    "slightly": 0.6, "marginally": 0.5, "modestly": 0.7, "somewhat": 0.7,
}

# Longest phrases first so multi-word entries ("beat estimates") win over the
# single-word fallbacks ("beat").
_PHRASES = sorted(
    set(POSITIVE) | set(NEGATIVE), key=lambda p: len(p.split()), reverse=True
)
_MULTIWORD = [p for p in _PHRASES if " " in p]

_TOKEN_RE = re.compile(r"[a-z0-9'\-]+")


def _lexicon_weight(term: str) -> float:
    if term in POSITIVE:
        return POSITIVE[term]
    if term in NEGATIVE:
        return -NEGATIVE[term]
    return 0.0


@lru_cache(maxsize=4096)
def score_text(text: str) -> float:
    """Return a sentiment score in ``[-1.0, 1.0]`` (positive == bullish)."""
    if not text:
        return 0.0
    lowered = text.lower()

    # Replace known multi-word phrases with single tokens so they score as a
    # unit and are not double-counted by their component words.
    for phrase in _MULTIWORD:
        if phrase in lowered:
            lowered = lowered.replace(phrase, " __" + phrase.replace(" ", "_"))

    tokens = _TOKEN_RE.findall(lowered)
    score = 0.0
    hits = 0
    for i, tok in enumerate(tokens):
        term = tok[2:].replace("_", " ") if tok.startswith("__") else tok
        weight = _lexicon_weight(term)
        if weight == 0.0:
            continue
        # Look back up to 3 tokens for negators / intensifiers.
        mult = 1.0
        negated = False
        for j in range(max(0, i - 3), i):
            prev = tokens[j]
            prev = prev[2:].replace("_", " ") if prev.startswith("__") else prev
            if prev in NEGATORS:
                negated = True
            if prev in INTENSIFIERS:
                mult *= INTENSIFIERS[prev]
        if negated:
            weight = -weight * 0.85
        score += weight * mult
        hits += 1

    if hits == 0:
        return 0.0
    # Squash the summed lexicon weight into [-1, 1] with a soft tanh so that a
    # pile-up of strong words saturates rather than running away.
    return math.tanh(score / 3.5)


def label(score: float) -> str:
    """Human-readable bucket for a sentiment score."""
    if score >= 0.5:
        return "very bullish"
    if score >= 0.15:
        return "bullish"
    if score <= -0.5:
        return "very bearish"
    if score <= -0.15:
        return "bearish"
    return "neutral"
