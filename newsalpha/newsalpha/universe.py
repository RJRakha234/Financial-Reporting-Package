"""Ticker universe: symbol <-> company-name mapping used to build news queries
and to filter headlines for relevance.

The default watchlist is a broad set of liquid large-caps so the tool does
something useful out of the box; pass your own tickers on the command line to
override it.
"""

from __future__ import annotations

# symbol -> (company name, list of extra aliases used for relevance matching)
COMPANIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "AAPL": ("Apple", ("iphone", "ipad", "mac")),
    "MSFT": ("Microsoft", ("azure", "windows", "copilot")),
    "GOOGL": ("Alphabet", ("google", "youtube", "waymo")),
    "AMZN": ("Amazon", ("aws", "prime")),
    "NVDA": ("Nvidia", ("gpu", "cuda", "blackwell")),
    "META": ("Meta Platforms", ("facebook", "instagram", "whatsapp")),
    "TSLA": ("Tesla", ("musk", "cybertruck", "model y")),
    "AMD": ("Advanced Micro Devices", ("ryzen", "radeon", "epyc")),
    "NFLX": ("Netflix", ()),
    "JPM": ("JPMorgan Chase", ("jpmorgan",)),
    "BAC": ("Bank of America", ()),
    "WMT": ("Walmart", ()),
    "DIS": ("Walt Disney", ("disney",)),
    "INTC": ("Intel", ()),
    "BA": ("Boeing", ()),
    "XOM": ("Exxon Mobil", ("exxon",)),
    "PFE": ("Pfizer", ()),
    "KO": ("Coca-Cola", ()),
    "CRM": ("Salesforce", ()),
    "ORCL": ("Oracle", ()),
}

DEFAULT_WATCHLIST: tuple[str, ...] = (
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD",
)


def company_name(ticker: str) -> str:
    """Best-effort company name for a ticker (falls back to the ticker)."""
    info = COMPANIES.get(ticker.upper())
    return info[0] if info else ticker.upper()


def aliases(ticker: str) -> tuple[str, ...]:
    """Lower-cased relevance keywords (company name + aliases + ticker)."""
    ticker = ticker.upper()
    info = COMPANIES.get(ticker)
    out = {ticker.lower()}
    if info:
        out.add(info[0].lower())
        out.update(a.lower() for a in info[1])
        # also the first word of the company name (e.g. "jpmorgan")
        out.add(info[0].split()[0].lower())
    return tuple(out)


def query_for(ticker: str) -> str:
    """Search query string for news feeds (company name improves recall)."""
    name = company_name(ticker)
    if name.upper() == ticker.upper():
        return f"{ticker} stock"
    return f"{name} ({ticker}) stock"
