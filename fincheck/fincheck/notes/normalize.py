"""Canonical topics, document kinds, and text normalization.

The comparison hinges on neutralizing the *expected* differences between the
four statements so that only genuine wording differences remain:

* **entity** — "the Company" (standalone) vs "the Group" (consolidated);
* **framework** — "Ind AS 116" (Ind AS) vs "IFRS 16"/"IAS 1" (IFRS);
* **currency** — "₹ ... crore" (INR) vs "US$ ... million" (USD), and the
  monetary amounts themselves, which differ by entity scope and FX.

These are precisely the "minor changes" the checker tolerates, so
:func:`fingerprint` collapses each of them to a neutral token. Two sentences
that differ *only* by such expected variation produce the same fingerprint and
are therefore treated as aligned; anything else surfaces as a difference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Document kinds
# --------------------------------------------------------------------------- #

_FRAMEWORKS = {"indas": "Ind AS", "ifrs": "IFRS"}
_ENTITIES = {"standalone": "Standalone", "consol": "Consolidated"}
_CURRENCIES = {"inr": "INR", "usd": "USD"}


@dataclass(frozen=True)
class DocKind:
    """How a document varies along the three expected-difference axes."""

    framework: str  # "indas" | "ifrs"
    entity: str  # "standalone" | "consol"
    currency: str  # "inr" | "usd"

    @property
    def label(self) -> str:
        return (
            f"{_ENTITIES[self.entity]} {_FRAMEWORKS[self.framework]} "
            f"({_CURRENCIES[self.currency]})"
        )

    @property
    def slug(self) -> str:
        return f"{self.framework}-{self.entity}-{self.currency}"


def parse_kind(spec: str) -> DocKind:
    """Parse a ``framework-entity-currency`` spec, e.g. ``ifrs-consol-usd``."""
    parts = [p.strip().lower() for p in re.split(r"[-_/]", spec) if p.strip()]
    framework = next((p for p in parts if p in _FRAMEWORKS), None)
    entity = next((p for p in parts if p in _ENTITIES), None)
    currency = next((p for p in parts if p in _CURRENCIES), None)
    if not (framework and entity and currency):
        raise ValueError(
            f"invalid doc kind {spec!r}; expected framework-entity-currency, "
            "e.g. 'ifrs-consol-usd' (framework: indas|ifrs, entity: "
            "standalone|consol, currency: inr|usd)"
        )
    return DocKind(framework, entity, currency)


def infer_kind(text: str) -> DocKind:
    """Best-effort inference of a :class:`DocKind` from a document's text.

    Works best on the cover / primary-statement pages, where the framework,
    "Standalone"/"Consolidated", and the currency unit caption ("(In ₹ crore)"
    vs "(Dollars in millions)") are stated plainly. Always overridable on the
    command line when a document's wording defeats the heuristics.
    """
    low = text.lower()

    n_ifrs = low.count("ifrs") + low.count("ias ") + low.count("international financial reporting")
    n_indas = low.count("ind as") + low.count("ind-as") + low.count("indian accounting standard")
    framework = "ifrs" if n_ifrs > n_indas else "indas"

    n_standalone = low.count("standalone")
    n_consol = low.count("consolidated")
    entity = "standalone" if n_standalone > n_consol else "consol"

    n_usd = low.count("us$") + low.count("usd") + low.count("dollar") + low.count("million")
    n_inr = low.count("₹") + low.count("crore") + low.count("inr") + low.count("rupee")
    currency = "usd" if n_usd > n_inr else "inr"

    return DocKind(framework, entity, currency)


# --------------------------------------------------------------------------- #
# Canonical topics — match notes across documents despite title variation
# --------------------------------------------------------------------------- #

# Explicit merges for genuine wording differences between frameworks. Keys and
# values are both *normalized* titles (see _normalize_title); the value is the
# canonical title the note is filed under. Upper/lower-case and punctuation
# variants already collapse via normalization, so only true wording differences
# need listing here.
_TITLE_ALIASES = {
    "goodwill and other intangible assets": "goodwill and intangible assets",
    "provisions and other contingencies": "provisions",
    "prepayments and other assets": "other assets",
    "prepayments and other current assets": "other assets",
    "break up of expenses and other income net": "expenses",
}

# Spelling / spacing unifications applied during title normalization.
_SPELLING = {"judgements": "judgments", "organisation": "organization"}

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _normalize_title(title: str) -> str:
    out = title.lower()
    out = _PUNCT_RE.sub(" ", out)
    out = _WS_RE.sub(" ", out).strip()
    for a, b in _SPELLING.items():
        out = re.sub(rf"\b{a}\b", b, out)
    return out


def canonical_topic(title: str) -> str:
    """Map a note title to a canonical key shared across documents.

    Titles that are identical modulo case/punctuation collapse automatically;
    known cross-framework wording variants are merged via ``_TITLE_ALIASES``.
    Unknown titles fall back to their normalized form, so two documents using
    the very same title still match.
    """
    norm = _normalize_title(title)
    return _TITLE_ALIASES.get(norm, norm)


# --------------------------------------------------------------------------- #
# Sentence splitting
# --------------------------------------------------------------------------- #

# Split after a period that ends a sentence: one followed by whitespace, an
# uppercase letter (run-together justified text), or end-of-string — but not a
# period inside a decimal/clause number like "116.5" or "2.10" (digit after).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=\.)(?=\s|[A-Z(])")


def split_sentences(text: str) -> list[str]:
    """Split narrative prose into sentence-like units.

    Robust to the missing-space justified text in these PDFs (periods survive
    even when spaces do not). Source line breaks are *not* sentence boundaries
    — the same prose wraps at different points in different documents — so they
    are folded to spaces and splitting happens only at sentence-ending periods.
    Returns trimmed, non-empty units.
    """
    stream = _WS_RE.sub(" ", text.replace("\n", " ")).strip()
    units: list[str] = []
    for piece in _SENTENCE_SPLIT_RE.split(stream):
        piece = piece.strip()
        if piece:
            units.append(piece)
    return units


# --------------------------------------------------------------------------- #
# Fingerprinting — neutralize expected differences
# --------------------------------------------------------------------------- #

# A standard reference: "Ind AS 116", "IFRS 16", "IAS 1", "Ind AS 116A".
#
# IMPORTANT: intra-word spacing is unreliable in these PDFs ("the Company" may
# extract as "theCompany"), so none of these patterns may rely on word
# boundaries — they match as substrings. The trailing-digit requirement on
# standard references keeps "ias"/"ifrs" from matching inside ordinary words.
_STD_REF_RE = re.compile(r"(?:ind\s*as|ifrs|ias)\s*\d+[a-z]*", re.IGNORECASE)
# Reporting-scope adjectives that differ standalone vs consolidated.
_SCOPE_RE = re.compile(r"standalone|consolidated|consolidation", re.IGNORECASE)
# Reporting entity: "the Company" (standalone) vs "the Group" (consolidated).
_ENTITY_RE = re.compile(r"company|group", re.IGNORECASE)
# Currency markers. Short abbreviations ("rs") are unsafe as substrings, so we
# rely on the symbols, the ISO-ish codes, and the units below.
_CURRENCY_RE = re.compile(r"₹|us\$|usd|inr|\$|€|£", re.IGNORECASE)
_UNIT_RE = re.compile(r"crores?|lakhs?|millions?|billions?|thousand", re.IGNORECASE)
# Any run of digits with separators / decimals — monetary amounts differ by
# scope and FX, so they are not comparable across these documents.
_NUMBER_RE = re.compile(r"\d[\d,.\s]*\d|\d")


# Neutralization patterns applied in priority order. Standard references and
# currency symbols come before bare numbers so their digits are not consumed
# first; the empty replacement for scope simply deletes the adjective.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (_STD_REF_RE, "§std"),
    (_SCOPE_RE, ""),
    (_ENTITY_RE, "§ent"),
    (_CURRENCY_RE, "§cur"),
    (_UNIT_RE, "§unit"),
    (_NUMBER_RE, "§num"),
]


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Canonical fingerprint of prose plus a map back to the source text.

    Returns ``(norm, src)`` where ``norm`` is the whitespace-free, neutralized
    fingerprint and ``src[k]`` is the index in ``text`` that ``norm[k]`` came
    from. The single-pass scan applies the same neutralizations as
    :func:`fingerprint`, so a diff computed on ``norm`` can be projected back
    onto the original (readable) prose for display.
    """
    norm: list[str] = []
    src: list[int] = []
    i, n = 0, len(text)
    while i < n:
        for rx, repl in _PATTERNS:
            m = rx.match(text, i)
            if m and m.end() > i:
                for ch in repl:
                    norm.append(ch)
                    src.append(i)
                i = m.end()
                break
        else:
            ch = text[i]
            if ch.isalnum():
                norm.append(ch.lower())
                src.append(i)
            i += 1
    return "".join(norm), src


def fingerprint(text: str) -> str:
    """Canonical form of prose with expected differences neutralized.

    The result has no whitespace (intra-word spacing is unreliable in the
    source PDFs) and collapses entity/framework/currency/amount variation to
    fixed tokens, so only substantive wording differences remain.
    """
    return normalize_with_map(text)[0]
