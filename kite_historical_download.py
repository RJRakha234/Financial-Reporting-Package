#!/usr/bin/env python3
"""Download historical OHLCV candles from Zerodha Kite Connect.

A standalone tool — it needs only ``kiteconnect``, and does not import the
nifty50 engine. Use it when you want raw candles on disk; use
``python -m nifty50.scripts.backfill`` when you want them in the engine's
parquet store with integrity checks and corporate-action handling.

Two Kite facts drive the whole design:

* **Tokens are daily and interactive.** There is no unattended re-auth. The
  ``login`` subcommand walks the OAuth redirect once a day; the resulting
  access token is cached so the ``download`` subcommand runs unattended for the
  rest of the session.
* **One request cannot span an arbitrary range.** The per-request day cap
  varies by interval (60 days for ``minute``, 2000 for ``day``), so any real
  backfill is many requests. This tool chunks, rate-limits at the vendor's
  3 req/s historical quota, and retries transient failures.

Bars are returned **unadjusted** — Kite's adjustment policy is not contractual.
Apply corporate actions downstream rather than assuming these are split-adjusted.

Usage
-----
    # once per day - prompts for credentials, prints a URL, you paste back
    # the request_token. The api_key and token are then cached for the day,
    # so `download` needs no environment variables at all.
    python kite_historical_download.py login

    python kite_historical_download.py download \\
        --symbols RELIANCE,INFY,TCS --interval day --from 2015-01-01

    python kite_historical_download.py download \\
        --symbols "NIFTY 50" --interval 5minute --from 2024-01-01 --format parquet

    python kite_historical_download.py symbols --search RELIANCE
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from getpass import getpass
from pathlib import Path
from typing import Any, Final

# --------------------------------------------------------------------------
# Vendor constants. These come from the Kite Connect docs; change them only
# when the vendor does.
# --------------------------------------------------------------------------

# Maximum span of a single historical request, in days, per interval.
CHUNK_DAYS: Final[dict[str, int]] = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}

# Historical API quota. Exceeding it earns HTTP 429s, not a friendly warning.
REQUESTS_PER_SECOND: Final[float] = 3.0

# Transient failures worth retrying, with exponential backoff.
RETRY_DELAYS: Final[tuple[int, ...]] = (2, 4, 8, 16)

SESSION_FILE: Final[Path] = Path(".kite_session.json")
INSTRUMENT_CACHE: Final[Path] = Path(".kite_instruments.json")


class DownloadError(RuntimeError):
    """Anything that should stop the run with a readable message."""


def parse_lookback(spec: str, end: dt.date) -> dt.date:
    """Turn a lookback like ``6m``, ``30d`` or ``2y`` into a start date.

    Months are calendar months back from ``end`` (clamped for short months),
    not a 30-day approximation, so "6m" on 15 Aug means 15 Feb.
    """
    text = spec.strip().lower()
    if len(text) < 2 or not text[:-1].isdigit():
        raise DownloadError(f"invalid --last {spec!r}; expected e.g. 30d, 6m, 2y")
    amount, unit = int(text[:-1]), text[-1]
    if amount <= 0:
        raise DownloadError(f"invalid --last {spec!r}; the amount must be positive")

    if unit == "d":
        return end - dt.timedelta(days=amount)
    if unit == "w":
        return end - dt.timedelta(weeks=amount)
    if unit in ("m", "y"):
        months = amount * 12 if unit == "y" else amount
        year, month = divmod((end.year * 12 + end.month - 1) - months, 12)
        month += 1
        # Clamp so "1m" from 31 Mar lands on 28/29 Feb rather than overflowing.
        last_day = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
        return dt.date(year, month, min(end.day, last_day))
    raise DownloadError(f"invalid --last {spec!r}; unit must be one of d, w, m, y")


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def _credential(name: str, *, interactive: bool, secret: bool = False) -> str:
    """Read a credential from the environment, else prompt for it.

    Setting environment variables is the one step that reliably trips people up
    across shells (``export`` in bash, ``set`` in cmd, ``$env:`` in PowerShell),
    so an interactive run asks rather than lecturing about syntax. Secrets are
    read through getpass so they never land in the terminal scrollback.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not interactive:
        raise DownloadError(
            f"{name} is not set. Run `login` first, or set it in your shell.\n"
            f"  cmd:        set {name}=...\n"
            f"  PowerShell: $env:{name}=\"...\"\n"
            f"  bash/zsh:   export {name}=..."
        )
    prompt = f"  {name}: "
    entered = (getpass(prompt) if secret else input(prompt)).strip()
    if not entered:
        raise DownloadError(f"{name} is required")
    return entered


def _load_session() -> dict[str, str]:
    """Return today's cached session, if it is still plausibly valid.

    Kite tokens die at ~06:00 IST. Rather than track that precisely, we cache
    the date the token was minted and discard it on any later date — a wasted
    re-login is cheaper than a confusing 403 mid-backfill.
    """
    if not SESSION_FILE.exists():
        return {}
    try:
        cached = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if cached.get("date") != _ist_today().isoformat():
        return {}
    return {str(k): str(v) for k, v in cached.items() if v}


def _cache_session(api_key: str, access_token: str) -> None:
    """Persist the token *and* the key, so `download` needs no environment."""
    SESSION_FILE.write_text(
        json.dumps(
            {
                "api_key": api_key,
                "access_token": access_token,
                "date": _ist_today().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        # The token is a live credential for a trading account.
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        # Windows ignores POSIX modes; not worth failing a good login over.
        pass


def _ist_today() -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)).date()


def make_client(*, interactive: bool) -> Any:
    """Return an authenticated KiteConnect client.

    With ``interactive`` set, walk the OAuth redirect when no usable token is
    cached. Without it, fail fast — an unattended run should not hang on stdin.
    """
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:  # pragma: no cover - depends on the user's env
        raise DownloadError("kiteconnect is not installed. Run: pip install kiteconnect") from exc

    session = _load_session()
    api_key = (
        os.environ.get("KITE_API_KEY", "").strip()
        or session.get("api_key", "")
        or _credential("KITE_API_KEY", interactive=interactive)
    )
    kite = KiteConnect(api_key=api_key)

    token = os.environ.get("KITE_ACCESS_TOKEN", "").strip() or session.get("access_token")
    if token:
        kite.set_access_token(token)
        try:
            profile = kite.profile()
        except Exception as exc:  # noqa: BLE001 - any failure means "re-login"
            if not interactive:
                raise DownloadError(
                    f"cached Kite token was rejected ({exc}). "
                    "Run: python kite_historical_download.py login"
                ) from exc
            print(f"Cached token rejected ({exc}); starting a fresh login.", file=sys.stderr)
        else:
            print(f"Authenticated as {profile.get('user_name')} ({profile.get('user_id')}).")
            return kite

    if not interactive:
        raise DownloadError(
            "No valid Kite access token. Kite tokens expire daily and cannot be refreshed "
            "unattended.\n  Run: python kite_historical_download.py login"
        )

    api_secret = _credential("KITE_API_SECRET", interactive=True, secret=True)
    print("\n  1. Open this URL and log in:\n")
    print(f"     {kite.login_url()}\n")
    print("  2. You land on your app's redirect URL. Copy the `request_token=...`")
    print("     value out of the address bar (it is single-use and short-lived).\n")
    request_token = input("  request_token: ").strip()
    if not request_token:
        raise DownloadError("no request_token supplied")

    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = str(data["access_token"])
    kite.set_access_token(access_token)
    _cache_session(api_key, access_token)
    print(f"\nLogged in as {data.get('user_name')} ({data.get('user_id')}).")
    print(f"Access token cached in {SESSION_FILE} (valid until ~06:00 IST tomorrow).")
    return kite


# --------------------------------------------------------------------------
# Instrument resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    exchange: str
    token: int
    name: str
    kind: str
    expiry: str = ""  # ISO date; empty for cash and index instruments

    @property
    def is_derivative(self) -> bool:
        return self.kind in {"FUT", "CE", "PE"}

    @property
    def expiry_date(self) -> dt.date | None:
        return dt.date.fromisoformat(self.expiry) if self.expiry else None


def _to_instrument(row: dict[str, Any], exchange: str) -> Instrument:
    expiry = row.get("expiry")
    return Instrument(
        symbol=str(row["tradingsymbol"]),
        exchange=exchange,
        token=int(row["instrument_token"]),
        name=str(row.get("name") or ""),
        kind=str(row.get("instrument_type") or ""),
        # The client parses this into a date; some masters hand back a string.
        expiry=expiry.isoformat() if isinstance(expiry, dt.date) else str(expiry or ""),
    )


def resolve_future(
    instruments: list[Instrument], underlying: str, *, on: dt.date, month: int = 1
) -> Instrument:
    """Pick a futures contract for ``underlying`` by expiry rank.

    Contract symbols carry their expiry (``RELIANCE25AUGFUT``), so any hardcoded
    name goes stale within a month. Resolving by underlying keeps a saved
    command working indefinitely. ``month=1`` is the front (nearest unexpired)
    contract, 2 the next, and so on.
    """
    wanted = underlying.strip().upper()
    chain = sorted(
        (
            i
            for i in instruments
            if i.kind == "FUT"
            and i.name.upper() == wanted
            and (i.expiry_date is None or i.expiry_date >= on)
        ),
        key=lambda i: i.expiry or "9999-12-31",
    )
    if not chain:
        available = sorted({i.name for i in instruments if i.kind == "FUT"})
        near = [n for n in available if wanted in n][:8]
        hint = f" Similar underlyings: {', '.join(near)}." if near else ""
        raise DownloadError(
            f"no unexpired futures found for {underlying!r} on this exchange.{hint}"
        )
    if month > len(chain):
        raise DownloadError(
            f"--futures-month {month} requested but only {len(chain)} contract(s) "
            f"listed for {underlying}: {', '.join(i.symbol for i in chain)}"
        )
    return chain[month - 1]


def load_instruments(kite: Any, exchange: str, *, refresh: bool = False) -> list[Instrument]:
    """Fetch the instrument master, cached for the day.

    The master is several MB and changes once a day at most, so re-downloading
    it per invocation just burns quota.
    """
    cache_key = f"{exchange}:{_ist_today().isoformat()}"
    if INSTRUMENT_CACHE.exists() and not refresh:
        try:
            cached = json.loads(INSTRUMENT_CACHE.read_text(encoding="utf-8"))
            if cached.get("key") == cache_key:
                return [Instrument(**row) for row in cached["instruments"]]
        except (OSError, json.JSONDecodeError, TypeError):
            pass  # a corrupt cache is not worth reporting; just refetch

    print(f"Fetching the {exchange} instrument master ...")
    rows: list[dict[str, Any]] = kite.instruments(exchange)
    instruments = [_to_instrument(row, exchange) for row in rows]
    INSTRUMENT_CACHE.write_text(
        json.dumps({"key": cache_key, "instruments": [asdict(i) for i in instruments]}),
        encoding="utf-8",
    )
    print(f"  {len(instruments)} instruments cached.")
    return instruments


def resolve(instruments: list[Instrument], symbol: str) -> Instrument:
    wanted = symbol.strip().upper()
    for instrument in instruments:
        if instrument.symbol.upper() == wanted:
            return instrument
    near = [i.symbol for i in instruments if wanted in i.symbol.upper()][:8]
    hint = f" Did you mean: {', '.join(near)}?" if near else ""
    raise DownloadError(f"{symbol!r} not found in the instrument master.{hint}")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


class RateLimiter:
    """Minimum-interval limiter. The vendor quota is per-second and strict."""

    def __init__(self, per_second: float) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


def date_chunks(
    start: dt.date, end: dt.date, span_days: int
) -> list[tuple[dt.date, dt.date]]:
    """Split ``[start, end]`` into inclusive windows of at most ``span_days``."""
    chunks: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor <= end:
        # -1 because both endpoints are inclusive: a 60-day cap spans 60 dates.
        window_end = min(cursor + dt.timedelta(days=span_days - 1), end)
        chunks.append((cursor, window_end))
        cursor = window_end + dt.timedelta(days=1)
    return chunks


def fetch_candles(
    kite: Any,
    instrument: Instrument,
    interval: str,
    start: dt.date,
    end: dt.date,
    *,
    limiter: RateLimiter,
    continuous: bool,
    oi: bool,
) -> list[dict[str, Any]]:
    """Fetch every candle in ``[start, end]``, chunked and rate-limited."""
    from kiteconnect import exceptions as kite_ex

    span = CHUNK_DAYS[interval]
    chunks = date_chunks(start, end, span)
    candles: list[dict[str, Any]] = []

    for index, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        # Full-day bounds so intraday intervals capture the whole session.
        from_dt = dt.datetime.combine(chunk_start, dt.time(0, 0, 0))
        to_dt = dt.datetime.combine(chunk_end, dt.time(23, 59, 59))

        for attempt, delay in enumerate((*RETRY_DELAYS, None)):
            limiter.wait()
            try:
                batch = kite.historical_data(
                    instrument_token=instrument.token,
                    from_date=from_dt,
                    to_date=to_dt,
                    interval=interval,
                    continuous=continuous,
                    oi=oi,
                )
                candles.extend(batch)
                print(
                    f"  [{index}/{len(chunks)}] {chunk_start} .. {chunk_end}: "
                    f"{len(batch)} candles",
                    flush=True,
                )
                break
            except kite_ex.TokenException as exc:
                # Retrying a dead token just wastes the remaining chunks.
                raise DownloadError(
                    f"session expired mid-download ({exc}). Re-run `login` and start again."
                ) from exc
            except kite_ex.InputException as exc:
                # Usually a range predating the instrument's listing. Not fatal.
                print(
                    f"  [{index}/{len(chunks)}] {chunk_start} .. {chunk_end}: skipped ({exc})",
                    file=sys.stderr,
                )
                break
            except Exception as exc:  # noqa: BLE001 - network/data/5xx are all retryable
                if delay is None:
                    raise DownloadError(
                        f"{instrument.symbol} {chunk_start}..{chunk_end} failed after "
                        f"{attempt} retries: {exc}"
                    ) from exc
                print(
                    f"  [{index}/{len(chunks)}] {exc} - retrying in {delay}s",
                    file=sys.stderr,
                )
                time.sleep(delay)

    return candles


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def write_candles(
    candles: list[dict[str, Any]],
    path: Path,
    fmt: str,
) -> int:
    """Write candles, de-duplicated and sorted by timestamp. Returns row count."""
    if not candles:
        return 0

    # Chunk boundaries can overlap by a bar; the timestamp is the natural key.
    unique: dict[Any, dict[str, Any]] = {}
    for candle in candles:
        unique[candle["date"]] = candle
    rows = [unique[key] for key in sorted(unique)]

    columns = ["date", "open", "high", "low", "close", "volume"]
    if any("oi" in row for row in rows):
        columns.append("oi")

    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "parquet":
        require_parquet_support()
        import pandas as pd

        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.tz_convert("Asia/Kolkata")
        frame = frame.reindex(columns=columns).set_index("date")
        frame.to_parquet(path)
    else:
        import csv

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                out = dict(row)
                # isoformat keeps the +05:30 offset that Kite returns.
                out["date"] = out["date"].isoformat()
                writer.writerow(out)

    return len(rows)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_login(_args: argparse.Namespace) -> int:
    print(
        "Kite login. Credentials come from the environment if set, otherwise you\n"
        "will be prompted. Find them at https://developers.kite.trade/apps\n"
    )
    make_client(interactive=True)
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    kite = make_client(interactive=False)
    instruments = load_instruments(kite, args.exchange, refresh=args.refresh)
    needle = args.search.strip().upper()
    matches = [
        i for i in instruments if needle in i.symbol.upper() or needle in i.name.upper()
    ][: args.limit]
    if not matches:
        print(f"No instrument on {args.exchange} matches {args.search!r}.")
        return 1
    width = max(len(i.symbol) for i in matches)
    for instrument in matches:
        print(f"{instrument.symbol:<{width}}  {instrument.token:>10}  {instrument.name}")
    return 0


def read_symbols_file(path: Path) -> list[str]:
    """Read trading symbols from a text or CSV file.

    Accepts one symbol per line, or a CSV with a ``symbol`` column. Blank lines
    and ``#`` comments are ignored. A file beats ``--symbols`` for the Nifty 50
    because ``M&M`` contains an ``&``, which cmd.exe treats as a command
    separator and PowerShell rejects outright.
    """
    if not path.exists():
        raise DownloadError(f"symbols file not found: {path}")
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise DownloadError(f"{path} contains no symbols")

    header = [column.strip().lower() for column in lines[0].split(",")]
    if "symbol" in header:
        index = header.index("symbol")
        rows = [line.split(",") for line in lines[1:]]
        symbols = [row[index].strip() for row in rows if len(row) > index and row[index].strip()]
    else:
        # Headerless: take the first field of each line.
        symbols = [line.split(",")[0].strip() for line in lines]

    seen: dict[str, None] = {}
    for symbol in symbols:
        seen.setdefault(symbol.upper(), None)
    if not seen:
        raise DownloadError(f"{path} contains no usable symbols")
    return list(seen)


def parse_intervals(spec: str) -> list[str]:
    """Expand a comma-separated interval list, or the ``intraday``/``all`` aliases.

    Returned in the declared order of :data:`CHUNK_DAYS` (coarsest cost first)
    rather than the order typed, so a large multi-interval run does its cheap
    work before spending quota on minute bars.
    """
    text = spec.strip().lower()
    if text == "all":
        wanted = list(CHUNK_DAYS)
    elif text == "intraday":
        wanted = [name for name in CHUNK_DAYS if name != "day"]
    else:
        wanted = [part.strip().lower() for part in text.split(",") if part.strip()]

    if not wanted:
        raise DownloadError("no interval given")
    unknown = [name for name in wanted if name not in CHUNK_DAYS]
    if unknown:
        raise DownloadError(
            f"unknown interval(s) {', '.join(unknown)}. "
            f"Valid: {', '.join(CHUNK_DAYS)}, or the aliases `intraday` / `all`"
        )
    return [name for name in CHUNK_DAYS if name in set(wanted)]


def cmd_download(args: argparse.Namespace) -> int:
    # Validate every argument before authenticating: a typo should not cost a
    # login round trip, and a half-validated run is worse than no run.
    intervals = parse_intervals(args.interval)
    if bool(args.symbols) == bool(args.symbols_file):
        raise DownloadError("give exactly one of --symbols or --symbols-file")
    symbols = (
        read_symbols_file(Path(args.symbols_file))
        if args.symbols_file
        else [s.strip() for s in args.symbols.split(",") if s.strip()]
    )
    if not symbols:
        raise DownloadError("no symbols given")
    if args.format == "parquet":
        require_parquet_support()
    if args.futures_month < 1:
        raise DownloadError("--futures-month must be 1 or greater")
    exchange = args.exchange or ("NFO" if args.futures else "NSE")

    end = args.to_date or _ist_today()
    if bool(args.from_date) == bool(args.last):
        raise DownloadError("give exactly one of --from or --last (e.g. --last 6m)")
    start = args.from_date or parse_lookback(args.last, end)
    if start > end:
        raise DownloadError(f"--from ({start}) is after --to ({end})")

    kite = make_client(interactive=False)
    instruments = load_instruments(kite, exchange, refresh=args.refresh)
    limiter = RateLimiter(REQUESTS_PER_SECOND)
    out_dir = Path(args.out)
    suffix = "parquet" if args.format == "parquet" else "csv"

    failures: list[str] = []
    total_rows = 0
    planned = sum(len(date_chunks(start, end, CHUNK_DAYS[i])) for i in intervals) * len(symbols)
    print(
        f"\n{len(symbols)} symbol(s) x {len(intervals)} interval(s) "
        f"({', '.join(intervals)}), {start} .. {end}\n"
        f"up to {planned} request(s), ~{planned / REQUESTS_PER_SECOND:.0f}s at "
        f"{REQUESTS_PER_SECOND:g} req/s"
    )

    for symbol in symbols:
        try:
            instrument = (
                resolve_future(instruments, symbol, on=end, month=args.futures_month)
                if args.futures
                else resolve(instruments, symbol)
            )
        except DownloadError as exc:
            print(f"{exc}", file=sys.stderr)
            failures.append(symbol)
            continue

        if args.futures:
            print(f"{symbol} -> {instrument.symbol} (expiry {instrument.expiry or 'n/a'})")
        # A continuous series is named after the underlying, not the contract —
        # the point of it is to outlive any one expiry. The -FUT suffix keeps it
        # from colliding with the cash file of the same name: RELIANCE the stock
        # and RELIANCE futures are different series and must not share a path.
        stem = f"{symbol}-FUT" if args.futures and args.continuous else instrument.symbol
        safe = stem.replace("/", "-").replace(" ", "_")

        for interval in intervals:
            label = f"{instrument.symbol} {interval}"
            path = out_dir / f"{safe}_{interval}.{suffix}"
            if path.exists() and not args.overwrite:
                print(f"{label}: {path} exists, skipping (use --overwrite)")
                continue

            chunks = len(date_chunks(start, end, CHUNK_DAYS[interval]))
            print(
                f"\n{instrument.symbol} ({instrument.exchange}, token {instrument.token}) "
                f"- {interval}, {start} .. {end}, {chunks} request(s)"
            )
            try:
                candles = fetch_candles(
                    kite,
                    instrument,
                    interval,
                    start,
                    end,
                    limiter=limiter,
                    continuous=args.continuous,
                    oi=args.oi or instrument.is_derivative,
                )
                written = write_candles(candles, path, args.format)
            except DownloadError as exc:
                # One dead pair should not abandon the rest of the run — and a
                # write failure must not discard what the other pairs fetched.
                print(f"  failed: {exc}", file=sys.stderr)
                failures.append(label)
                continue

            total_rows += written
            if written:
                print(f"  wrote {written} candles -> {path}")
            else:
                print(f"  no data returned for {label}", file=sys.stderr)

    print(
        f"\nDone. {total_rows} candles, {len(symbols)} symbol(s) "
        f"x {len(intervals)} interval(s)."
    )
    if failures:
        print(f"Failed ({len(failures)}): {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


def require_parquet_support() -> None:
    """Fail before any network work if parquet output is not installable here.

    write_candles() runs *after* fetch_candles(), so without this check a
    missing engine surfaces only once a symbol has already been downloaded —
    quota spent to learn about a local dependency.
    """
    missing = []
    try:
        import pandas as pd
    except ImportError:
        missing.append("pandas")
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        try:
            import fastparquet  # noqa: F401
        except ImportError:
            missing.append("pyarrow")
    if missing:
        raise DownloadError(
            f"--format parquet needs {' and '.join(missing)}.\n"
            f"  pip install {' '.join(missing)}\n"
            "Or drop --format parquet to write CSV instead (larger, but no extra deps)."
        )

    # Importing both is not proof they work together: mismatched pandas/pyarrow
    # versions import cleanly and then fail inside to_parquet. Do a real
    # round trip in memory so that surfaces here rather than mid-download.
    from io import BytesIO

    try:
        pd.DataFrame({"probe": [1]}).to_parquet(BytesIO())
    except Exception as exc:  # noqa: BLE001 - any engine failure disqualifies parquet
        raise DownloadError(
            f"pandas and the parquet engine are installed but incompatible:\n"
            f"  {type(exc).__name__}: {exc}\n"
            "Upgrading usually fixes it:\n"
            "  pip install -U pandas pyarrow\n"
            "Or drop --format parquet to write CSV instead (larger, but no extra deps)."
        ) from exc


def _scan_csv(path: Path) -> tuple[int, str, str]:
    """Return (rows, first timestamp, last timestamp) without loading the file.

    These files reach gigabytes at minute resolution, so the scan counts
    newlines over a binary buffer and keeps only the two edge lines.
    """
    rows = 0
    first = last = ""
    with path.open("rb") as handle:
        handle.readline()  # header
        for line in handle:
            if not line.strip():
                continue
            rows += 1
            if rows == 1:
                first = line.split(b",", 1)[0].decode("utf-8", "replace")
            last = line
    if rows:
        last = last.split(b",", 1)[0].decode("utf-8", "replace")  # type: ignore[union-attr]
    return rows, first, str(last)


def cmd_verify(args: argparse.Namespace) -> int:
    """Inventory downloaded files and flag ones whose coverage looks wrong.

    Worth running after any large pull: files are skipped when they already
    exist, so a directory can quietly mix windows from different runs.
    """
    directory = Path(args.dir)
    if not directory.exists():
        raise DownloadError(f"directory not found: {directory}")
    files = sorted(p for p in directory.iterdir() if p.suffix in (".csv", ".parquet"))
    if not files:
        raise DownloadError(f"no .csv or .parquet files in {directory}")

    expected_start: dt.date | None = None
    end = args.to_date or _ist_today()
    if args.last:
        expected_start = parse_lookback(args.last, end)
    elif args.from_date:
        expected_start = args.from_date

    print(f"{len(files)} file(s) in {directory}")
    if expected_start:
        print(f"expecting coverage from {expected_start}\n")

    rows_total = 0
    short: list[str] = []
    empty: list[str] = []

    for path in files:
        if path.suffix == ".parquet":
            require_parquet_support()
            import pandas as pd

            frame = pd.read_parquet(path)
            rows = len(frame)
            first = str(frame.index.min()) if rows else ""
            last = str(frame.index.max()) if rows else ""
        else:
            rows, first, last = _scan_csv(path)

        rows_total += rows
        if not rows:
            empty.append(path.name)
            print(f"  {path.name:<34} EMPTY")
            continue

        flag = ""
        if expected_start:
            actual = dt.date.fromisoformat(first[:10])
            # A month's grace absorbs the leading non-trading days; anything
            # beyond that is either a stale file or a later listing date.
            if (actual - expected_start).days > 31:
                flag = f"  <-- starts {(actual - expected_start).days}d late"
                short.append(path.name)
        print(f"  {path.name:<34} {rows:>9,d}  {first[:10]} .. {last[:10]}{flag}")

    print(f"\n{rows_total:,d} rows across {len(files)} file(s).")
    if empty:
        print(f"\n{len(empty)} empty file(s): {', '.join(empty[:10])}"
              f"{' ...' if len(empty) > 10 else ''}")
    if short:
        print(
            f"\n{len(short)} file(s) start later than requested. Either the symbol "
            f"listed after {expected_start}, or the file is left over from an earlier "
            f"run with a shorter window — re-download those with --overwrite:\n"
            f"  {', '.join(short[:10])}{' ...' if len(short) > 10 else ''}"
        )
    return 1 if (short or empty) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="walk the daily Kite OAuth flow and cache the token")
    login.set_defaults(func=cmd_login)

    symbols = sub.add_parser("symbols", help="search the instrument master for a trading symbol")
    symbols.add_argument("--search", required=True, help="substring of a symbol or company name")
    symbols.add_argument("--exchange", default="NSE")
    symbols.add_argument("--limit", type=int, default=25)
    symbols.add_argument("--refresh", action="store_true", help="bypass the instrument cache")
    symbols.set_defaults(func=cmd_symbols)

    download = sub.add_parser("download", help="download historical candles")
    download.add_argument("--symbols", default=None, help="comma-separated trading symbols")
    download.add_argument(
        "--symbols-file", dest="symbols_file", default=None, metavar="PATH",
        help="file of symbols, one per line or a CSV with a `symbol` column",
    )
    download.add_argument(
        "--interval", default="day",
        help=(
            f"comma-separated; one or more of: {', '.join(CHUNK_DAYS)}. "
            "Aliases: `intraday` (every interval below day), `all`. Default: day"
        ),
    )
    download.add_argument(
        "--from", dest="from_date", default=None, type=dt.date.fromisoformat,
        help="start date, YYYY-MM-DD",
    )
    download.add_argument(
        "--last", default=None, metavar="SPEC",
        help="lookback instead of --from: 30d, 6w, 6m, 2y",
    )
    download.add_argument(
        "--to", dest="to_date", default=None, type=dt.date.fromisoformat,
        help="end date, YYYY-MM-DD (default: today)",
    )
    download.add_argument(
        "--exchange", default=None, help="default: NSE, or NFO when --futures is given"
    )
    download.add_argument("--out", default="data/historical", help="output directory")
    download.add_argument("--format", choices=("csv", "parquet"), default="csv")
    download.add_argument("--overwrite", action="store_true")
    download.add_argument("--refresh", action="store_true", help="bypass the instrument cache")
    download.add_argument(
        "--continuous", action="store_true",
        help="stitch a continuous series across expiries (futures/options only)",
    )
    download.add_argument(
        "--oi", action="store_true",
        help="include open interest (implied for FUT/CE/PE)",
    )
    download.add_argument(
        "--futures", action="store_true",
        help="treat --symbols as underlyings and resolve each to a futures contract "
             "(implies --exchange NFO unless given). Pair with --continuous for a "
             "series that survives expiry rolls.",
    )
    download.add_argument(
        "--futures-month", dest="futures_month", type=int, default=1, metavar="N",
        help="which contract to take with --futures: 1 = front month (default), 2 = next",
    )
    download.set_defaults(func=cmd_download)

    verify = sub.add_parser(
        "verify", help="inventory downloaded files and flag suspect coverage"
    )
    verify.add_argument("--dir", default="data/historical")
    verify.add_argument(
        "--last", default=None, metavar="SPEC", help="window the files should cover, e.g. 72m"
    )
    verify.add_argument("--from", dest="from_date", default=None, type=dt.date.fromisoformat)
    verify.add_argument("--to", dest="to_date", default=None, type=dt.date.fromisoformat)
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except DownloadError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
