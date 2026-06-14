"""Fetch real-time stock news from public RSS feeds — no API key required.

Sources are keyless RSS endpoints (Yahoo Finance per-symbol headlines and a
Google-News keyword search). Everything uses the standard library only, so
there is nothing to sign up for. Each source is fetched defensively: a failure
on one source or one ticker never aborts the run.

Set ``NEWSALPHA_FEEDS`` to a comma-separated list of URL templates to override
the defaults (``{ticker}`` and ``{query}`` are substituted; ``{query}`` is
URL-encoded).
"""

from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from . import universe

USER_AGENT = (
    "Mozilla/5.0 (compatible; newsalpha/1.0; +https://example.invalid/newsalpha)"
)

# URL templates. {ticker} -> raw symbol, {query} -> URL-encoded search query.
DEFAULT_SOURCES: tuple[str, ...] = (
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Article:
    """A single news item attached to a ticker."""

    ticker: str
    title: str
    summary: str
    source: str
    url: str
    published: datetime | None

    @property
    def text(self) -> str:
        """Title + summary, the text that gets scored for sentiment."""
        return f"{self.title}. {self.summary}".strip()

    def age_hours(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        if self.published is None:
            return 48.0  # unknown timestamp: treat as moderately stale
        return max(0.0, (now - self.published).total_seconds() / 3600.0)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:  # ISO-8601 (Atom)
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_feed(xml_bytes: bytes, ticker: str) -> list[Article]:
    """Parse RSS 2.0 or Atom bytes into Articles (best-effort, never raises)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    articles: list[Article] = []
    # Find <item> (RSS) and <entry> (Atom) elements anywhere in the tree.
    for node in root.iter():
        tag = _strip_ns(node.tag)
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        link = ""
        for child in node:
            ctag = _strip_ns(child.tag)
            if ctag == "link":
                link = child.get("href") or _clean(child.text) or link
            else:
                fields[ctag] = _clean(child.text)
        title = fields.get("title", "")
        if not title:
            continue
        summary = fields.get("description") or fields.get("summary") or ""
        published = _parse_date(
            fields.get("pubDate") or fields.get("published") or fields.get("updated")
        )
        source = fields.get("source") or _domain(link)
        articles.append(
            Article(
                ticker=ticker.upper(),
                title=title,
                summary=summary,
                source=source,
                url=link,
                published=published,
            )
        )
    return articles


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _build_url(template: str, ticker: str) -> str:
    query = urllib.parse.quote(universe.query_for(ticker))
    return template.format(ticker=urllib.parse.quote(ticker), query=query)


def _sources() -> tuple[str, ...]:
    env = os.environ.get("NEWSALPHA_FEEDS")
    if env:
        return tuple(s.strip() for s in env.split(",") if s.strip())
    return DEFAULT_SOURCES


def _fetch_url(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _is_relevant(article: Article, ticker: str) -> bool:
    """Keep articles that actually mention the company (Google search can be
    noisy). Yahoo's per-symbol feed is trusted as-is."""
    haystack = article.text.lower()
    return any(alias in haystack for alias in universe.aliases(ticker))


def fetch_news(
    tickers: list[str],
    *,
    since_hours: float = 48.0,
    timeout: float = 10.0,
    sources: tuple[str, ...] | None = None,
    now: datetime | None = None,
    on_error=None,
) -> list[Article]:
    """Fetch and de-duplicate recent news for ``tickers`` from RSS sources.

    Args:
        tickers: list of symbols (e.g. ``["AAPL", "NVDA"]``).
        since_hours: drop articles older than this many hours.
        timeout: per-request timeout in seconds.
        sources: URL templates to use (defaults to Yahoo + Google News).
        now: reference time (defaults to current UTC; injectable for tests).
        on_error: optional callable ``(url, exception)`` for per-source errors.

    Returns:
        A flat list of de-duplicated, relevance-filtered ``Article`` objects.
    """
    now = now or datetime.now(timezone.utc)
    sources = sources or _sources()
    seen: set[str] = set()
    out: list[Article] = []

    for ticker in tickers:
        ticker = ticker.upper()
        for template in sources:
            url = _build_url(template, ticker)
            try:
                raw = _fetch_url(url, timeout)
            except Exception as exc:  # network/HTTP errors must not abort the run
                if on_error is not None:
                    on_error(url, exc)
                continue
            for art in parse_feed(raw, ticker):
                if not _is_relevant(art, ticker):
                    continue
                if art.age_hours(now) > since_hours:
                    continue
                key = (art.ticker, _norm_title(art.title))
                dkey = "\x1f".join(key)
                if dkey in seen:
                    continue
                seen.add(dkey)
                out.append(art)
    return out


def _norm_title(title: str) -> str:
    return _WS_RE.sub(" ", re.sub(r"[^a-z0-9 ]", "", title.lower())).strip()
