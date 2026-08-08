"""Read historical OHLCV exports that were not produced by this project.

A folder of vendor CSVs — a broker download, a Kaggle dump, a scraped archive —
is the most common way historical Indian equity data arrives, and it is the
most common way a backtest quietly ends up wrong. Every one of these failures
has been seen in real files:

*Timestamps with no timezone.* NSE data exported through a US-configured tool
comes back shifted by 9h30m or 10h30m depending on DST. It still looks like a
plausible session, just one that opens at 23:45.

*Prices adjusted, unadjusted, or half-adjusted.* Some vendors adjust the close
for splits but leave open/high/low raw, which produces bars where the close
sits outside the high. Others adjust prices but not volume, which makes every
pre-split volume feature wrong by the split ratio.

*Dates in DD-MM-YYYY.* Read as MM-DD-YYYY, 5 March becomes 3 May and only the
days past the 12th of a month fail to parse — so roughly two thirds of the
file loads fine and the rest is silently misdated.

So this module refuses to guess where guessing is dangerous. It reports what
it found and what it could not determine, and the caller decides. Nothing here
writes to the bar store; ingestion is a separate, deliberate step.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from nifty50.domain import BAR_INDEX_NAME, IST, Timeframe

# Column-name synonyms seen across Indian data vendors, Yahoo, Kaggle dumps and
# broker exports. Matching is case-insensitive and ignores spaces/underscores.
_COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "timestamp": (
        "timestamp", "datetime", "date", "time", "dateandtime", "tradingdate",
        "bardatetime", "index", "ts", "date1", "timestamp1", "tradedate",
    ),
    "open": ("open", "o", "openprice", "op"),
    "high": ("high", "h", "highprice", "hi"),
    "low": ("low", "l", "lowprice", "lo"),
    "close": ("close", "c", "closeprice", "cl", "lasttradedprice", "ltp"),
    "volume": (
        "volume", "v", "vol", "qty", "quantity", "tradedquantity",
        "totaltradedquantity", "ttltrdqnty",
    ),
}

# Columns that are informative but not part of the OHLCV contract. Kept when
# present because delivery percentage in particular feeds features/flow.py.
_OPTIONAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "adjusted_close": ("adjclose", "adjustedclose", "closeadj"),
    "trades": ("trades", "numberoftrades", "notrades"),
    "turnover": ("turnover", "value", "tradedvalue", "ttltrdval", "turnoverlacs"),
    "delivery_qty": (
        "deliveryqty", "deliverablequantity", "delivqty", "deliverable", "delivqty",
    ),
    "delivery_pct": (
        "deliverypct", "delivper", "percentdelivery", "deliverypercentage",
    ),
    "symbol": ("symbol", "ticker", "scrip", "name", "instrument"),
    "series": ("series",),
}

_REQUIRED = ("timestamp", "open", "high", "low", "close")

# Formats that cannot be read two ways. A four-digit leading year fixes the
# field order by definition, and a spelled month ("03-Jan-2019") names itself.
# Checking for these first matters because pandas, asked to parse "2019-01-02"
# with dayfirst=True, reads it as YYYY-DD-MM -- so a naive day-first/month-first
# comparison flags every ISO file in existence as ambiguous.
_ISO_LEADING_YEAR = re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
_SPELLED_MONTH = re.compile(r"[A-Za-z]{3}")

# Timeframe tokens vendors append to a filename: RELIANCE_15minute, SBIN_day,
# WIPRO_minute, TCS_60minute. Matched so the symbol can be recovered without
# destroying hyphens and ampersands that belong to it.
_TIMEFRAME_SUFFIX = re.compile(r"\d*(MINUTE|MIN|HOUR|DAY|EOD|DAILY)S?", re.IGNORECASE)

# Spacings we recognise, in minutes; D1 is handled separately because a daily
# bar's spacing is a calendar gap, not a fixed duration.
_TIMEFRAME_BY_MINUTES: dict[int, Timeframe] = {
    1: Timeframe.M1,
    5: Timeframe.M5,
    15: Timeframe.M15,
    30: Timeframe.M30,
    60: Timeframe.H1,
}


class VendorCsvError(ValueError):
    """The file could not be read as OHLCV bars at all."""


@dataclass(frozen=True, slots=True)
class LoadResult:
    """Bars plus everything the loader had to infer to produce them.

    The inferences are returned rather than applied silently because each one
    is a place the file could be misread, and a caller deciding whether to
    trust a backtest needs to see them.
    """

    path: Path
    frame: pd.DataFrame
    timeframe: Timeframe | None
    column_mapping: dict[str, str]
    extra_columns: dict[str, str]
    inferences: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()

    @property
    def rows(self) -> int:
        return len(self.frame)

    @property
    def span(self) -> tuple[dt.date, dt.date] | None:
        if self.frame.empty:
            return None
        index = self.frame.index
        return index[0].date(), index[-1].date()

    @property
    def symbol_hint(self) -> str | None:
        """Best guess at the instrument, from a symbol column or the filename."""
        if "symbol" in self.extra_columns:
            values = self.frame.attrs.get("symbols", [])
            if len(values) == 1:
                return str(values[0]).upper()
        # Strip a trailing timeframe token only. Splitting on every separator
        # mangles real NSE symbols: BAJAJ-AUTO contains a hyphen and M&M an
        # ampersand, and "BAJAJ-AUTO_15minute" reduced to "BAJAJ" -- a symbol
        # that does not exist. The panel would then carry a phantom name and
        # silently drop the real one.
        stem = self.path.stem.upper()
        if "_" in stem:
            head, tail = stem.rsplit("_", 1)
            if _TIMEFRAME_SUFFIX.fullmatch(tail):
                stem = head
        return stem or None


def load_vendor_file(path: Path, *, assume_ist: bool = True) -> LoadResult:
    """Read one OHLCV file -- delimited text or Parquet -- into the frame contract.

    Parquet is the sensible format for a full index history: 50 symbols across
    five timeframes is tens of millions of rows, and Parquet stores it at
    roughly a tenth the size of CSV with the dtypes preserved. It also removes
    the entire class of text-parsing hazards this module exists to guard
    against -- a Parquet timestamp column carries its own type and timezone, so
    there is no DD/MM ambiguity to resolve and nothing to sniff.

    Everything downstream of the read is identical, because the checks that
    matter -- adjustment status, calendar alignment, OHLC validity -- are
    properties of the data, not of the container it arrived in.
    """
    raw = _read_any(path)
    if raw.empty:
        raise VendorCsvError(f"{path.name}: no rows")

    mapping, extras = _map_columns(list(raw.columns))
    missing = [name for name in _REQUIRED if name not in mapping]
    if missing:
        raise VendorCsvError(
            f"{path.name}: no column matched {missing}. Columns present: {list(raw.columns)}"
        )

    inferences: list[str] = []
    concerns: list[str] = []

    timestamps, date_notes = _parse_timestamps(raw[mapping["timestamp"]], path)
    inferences.extend(date_notes)

    if timestamps.dt.tz is None:
        if assume_ist:
            timestamps = timestamps.dt.tz_localize(IST)
            inferences.append("timestamps were naive; localised to IST (Asia/Kolkata)")
        else:
            raise VendorCsvError(f"{path.name}: naive timestamps and assume_ist=False")
    else:
        timestamps = timestamps.dt.tz_convert(IST)
        inferences.append("timestamps carried an offset; converted to IST")

    # Built first, indexed second. Passing ``index=`` to the constructor
    # *reindexes* each component Series from its RangeIndex onto the new
    # DatetimeIndex, which matches nothing and silently yields an all-NaN
    # frame -- no error, no warning, just a file that reads as entirely
    # missing prices.
    frame = pd.DataFrame(
        {
            name: pd.to_numeric(raw[mapping[name]], errors="coerce").to_numpy()
            for name in ("open", "high", "low", "close")
        }
    )
    frame.index = pd.DatetimeIndex(timestamps.to_numpy(), name=BAR_INDEX_NAME)
    if "volume" in mapping:
        frame["volume"] = (
            pd.to_numeric(raw[mapping["volume"]], errors="coerce").fillna(0).to_numpy()
        )
    else:
        frame["volume"] = 0
        concerns.append(
            "no volume column: every volume-derived feature (VWAP, OBV, relative "
            "volume, the session-matched z-score) will be meaningless"
        )

    for canonical, source in extras.items():
        if canonical in ("symbol", "series"):
            continue
        frame[canonical] = pd.to_numeric(raw[source], errors="coerce").to_numpy()

    if "symbol" in extras:
        frame.attrs["symbols"] = sorted(set(raw[extras["symbol"]].dropna().astype(str)))
    if "series" in extras:
        series_values = sorted(set(raw[extras["series"]].dropna().astype(str).str.strip()))
        frame.attrs["series"] = series_values
        if series_values and series_values != ["EQ"]:
            concerns.append(
                f"series column contains {series_values}; only EQ is the ordinary "
                "equity segment — BE/BZ rows are trade-to-trade or surveillance"
            )

    frame = frame.sort_index()
    timeframe = infer_timeframe(pd.DatetimeIndex(frame.index))
    if timeframe is Timeframe.D1:
        frame, restamped = conform_daily_to_session_open(frame)
        if restamped:
            inferences.append(restamped)
    if timeframe is None:
        concerns.append(
            "could not infer a timeframe from the timestamp spacing; the file may "
            "mix resolutions or have too many gaps"
        )

    concerns.extend(_shape_concerns(frame, raw, mapping))
    return LoadResult(
        path=path,
        frame=frame,
        timeframe=timeframe,
        column_mapping=mapping,
        extra_columns=extras,
        inferences=tuple(inferences),
        concerns=tuple(concerns),
    )


def conform_daily_to_session_open(
    frame: pd.DataFrame, session_open: dt.time = dt.time(9, 15)
) -> tuple[pd.DataFrame, str | None]:
    """Restamp midnight-dated daily bars onto the session open.

    Vendor daily files carry a *date*, not a timestamp, so localising gives
    00:00 IST. The project's contract — and everything in
    :mod:`nifty50.trading_calendar` — stamps a bar at the instant its interval
    begins, which for a daily bar is the session open. Left at midnight, every
    daily file reports 100% missing bars and 100% unexpected bars against the
    calendar simultaneously, which is noise rather than a finding.

    Only applied when *every* timestamp is exactly midnight. A file with real
    intraday times is left alone.
    """
    if frame.empty:
        return frame, None
    index = pd.DatetimeIndex(frame.index)
    if not bool(((index.hour == 0) & (index.minute == 0) & (index.second == 0)).all()):
        return frame, None
    shifted = frame.copy()
    shifted.index = index + pd.Timedelta(
        hours=session_open.hour, minutes=session_open.minute
    )
    shifted.index.name = frame.index.name
    return shifted, (
        f"daily bars were date-only (midnight); restamped to the {session_open:%H:%M} "
        "session open to match the calendar's bar-start convention"
    )


def load_vendor_csv(path: Path, *, assume_ist: bool = True) -> LoadResult:
    """Backwards-compatible alias for :func:`load_vendor_file`."""
    return load_vendor_file(path, assume_ist=assume_ist)


def _read_any(path: Path) -> pd.DataFrame:
    """Read CSV/TSV or Parquet into a flat frame with the timestamp as a column.

    Parquet writers commonly persist the bar timestamp as the *index* rather
    than a column. Left there, the column mapper sees only OHLCV and rejects
    the file for having no timestamp -- so the index is promoted first, using
    its own name when it has one.
    """
    suffix = path.suffix.lower()
    if suffix in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
        if isinstance(frame.index, pd.DatetimeIndex):
            frame = frame.reset_index()
            if frame.columns[0] in (None, "index", ""):
                frame = frame.rename(columns={frame.columns[0]: "timestamp"})
        return frame
    return pd.read_csv(path, sep=_sniff_delimiter(path), engine="python")


def infer_timeframe(index: pd.DatetimeIndex) -> Timeframe | None:
    """Infer the bar size from the modal spacing between consecutive bars.

    The *modal* gap, not the mean or the minimum: overnight and weekend gaps
    would drag a mean far above the true bar size, and a single duplicated
    timestamp would drag the minimum to zero.
    """
    if len(index) < 3:
        return None
    deltas = pd.Series(index).diff().dropna()
    positive = deltas[deltas > pd.Timedelta(0)]
    if positive.empty:
        return None
    modal = positive.mode().iloc[0]

    if modal >= pd.Timedelta(hours=12):
        return Timeframe.D1
    minutes = round(modal.total_seconds() / 60)
    return _TIMEFRAME_BY_MINUTES.get(minutes)


def _sniff_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8", errors="replace")[:8192]
    try:
        return str(csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter)
    except csv.Error:
        return ","


def _normalise(name: str) -> str:
    return "".join(character for character in str(name).lower() if character.isalnum())


def _map_columns(columns: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Match source columns to canonical names, first match wins."""
    normalised = {_normalise(column): column for column in columns}
    mapping: dict[str, str] = {}
    for canonical, synonyms in _COLUMN_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in normalised:
                mapping[canonical] = normalised[synonym]
                break
    extras: dict[str, str] = {}
    for canonical, synonyms in _OPTIONAL_SYNONYMS.items():
        for synonym in synonyms:
            if synonym in normalised and normalised[synonym] not in mapping.values():
                extras[canonical] = normalised[synonym]
                break
    return mapping, extras


def _parse_timestamps(column: pd.Series, path: Path) -> tuple[pd.Series, list[str]]:
    """Parse dates, resolving the DD/MM versus MM/DD ambiguity explicitly.

    Pandas will happily parse ``03-05-2024`` under either interpretation. When
    a file contains no day above 12, both readings succeed and produce
    different data, so that case is reported rather than resolved — the caller
    has to know.
    """
    notes: list[str] = []
    text = column.astype(str).str.strip()

    sample = text.head(200)
    unambiguous = bool(
        sample.str.match(_ISO_LEADING_YEAR).mean() > 0.9
        or sample.str.contains(_SPELLED_MONTH).mean() > 0.9
    )
    if unambiguous:
        parsed = pd.to_datetime(text, errors="coerce", format="mixed")
        notes.append("dates were unambiguous (leading four-digit year or named month)")
        unparsed_iso = int(parsed.isna().sum())
        if unparsed_iso:
            notes.append(f"{unparsed_iso} rows had unparseable timestamps and became NaT")
        if int(parsed.notna().sum()) == 0:
            raise VendorCsvError(f"{path.name}: no timestamp column value could be parsed")
        return parsed, notes

    dayfirst = pd.to_datetime(text, dayfirst=True, errors="coerce", format="mixed")
    monthfirst = pd.to_datetime(text, dayfirst=False, errors="coerce", format="mixed")

    day_ok = int(dayfirst.notna().sum())
    month_ok = int(monthfirst.notna().sum())

    if day_ok == 0 and month_ok == 0:
        raise VendorCsvError(f"{path.name}: no timestamp column value could be parsed")

    if month_ok > day_ok:
        chosen, label = monthfirst, "month-first (MM/DD)"
    elif day_ok > month_ok:
        chosen, label = dayfirst, "day-first (DD/MM)"
    else:
        # Both parse everything. Indian vendors overwhelmingly write DD-MM-YYYY,
        # so that is the default — but it is a coin flip on the data alone.
        chosen, label = dayfirst, "day-first (DD/MM)"
        if not dayfirst.equals(monthfirst):
            notes.append(
                "AMBIGUOUS DATES: this file parses under both DD/MM and MM/DD and "
                "the two disagree. Assumed DD/MM (the Indian convention). If the "
                "vendor is US-based, every date with a day <= 12 is wrong."
            )

    notes.append(f"dates parsed as {label}")
    unparsed = int(chosen.isna().sum())
    if unparsed:
        notes.append(f"{unparsed} rows had unparseable timestamps and became NaT")
    return chosen, notes


def _shape_concerns(frame: pd.DataFrame, raw: pd.DataFrame, mapping: dict[str, str]) -> list[str]:
    """Cheap structural checks that do not need a trading calendar."""
    concerns: list[str] = []

    undated = int(pd.isna(pd.DatetimeIndex(frame.index)).sum())
    if undated:
        concerns.append(f"{undated} rows have no usable timestamp")

    prices = frame[["open", "high", "low", "close"]]
    if prices.isna().any().any():
        concerns.append(
            f"{int(prices.isna().any(axis=1).sum())} rows have a non-numeric or "
            "missing price field"
        )
    non_positive = int((prices <= 0).any(axis=1).sum())
    if non_positive:
        concerns.append(f"{non_positive} rows have a zero or negative price")

    if frame["volume"].lt(0).any():
        concerns.append("negative volume present")

    zero_volume = int((frame["volume"] == 0).sum())
    if zero_volume and len(frame):
        share = zero_volume / len(frame)
        if share > 0.05:
            concerns.append(
                f"{share:.1%} of bars have zero volume — either genuinely untraded "
                "intervals or a broken volume column"
            )

    # Prices adjusted on the close but not the rest is the classic half-adjusted
    # export, and it shows up as the close escaping the bar's own range.
    outside = int(
        (
            (frame["close"] > frame["high"] + 1e-9) | (frame["close"] < frame["low"] - 1e-9)
        ).sum()
    )
    if outside:
        concerns.append(
            f"{outside} bars have a close outside their own high-low range. This is "
            "the signature of a file whose close was back-adjusted for corporate "
            "actions while open/high/low were left raw."
        )

    if "adjusted_close" in frame.columns:
        difference = (frame["adjusted_close"] - frame["close"]).abs()
        if float(difference.max()) > 1e-6:
            concerns.append(
                "an adjusted-close column is present and differs from close, so this "
                "file is UNADJUSTED. Corporate actions must be applied before use."
            )
        else:
            concerns.append(
                "adjusted close equals close throughout, which means either the file "
                "is already adjusted or the instrument had no actions in this span"
            )
    return concerns


@dataclass(frozen=True, slots=True)
class DirectoryScan:
    """Everything found in a directory of vendor files."""

    root: Path
    loaded: tuple[LoadResult, ...] = ()
    failed: tuple[tuple[Path, str], ...] = ()
    skipped: tuple[Path, ...] = field(default=())

    def for_symbol(self, symbol: str) -> list[LoadResult]:
        wanted = symbol.upper()
        return [result for result in self.loaded if (result.symbol_hint or "") == wanted]


def scan_directory(
    root: Path, *, patterns: tuple[str, ...] = ("*.csv", "*.txt", "*.parquet", "*.pq")
) -> DirectoryScan:
    """Load every delimited file under ``root``, recording what failed and why."""
    if not root.exists():
        raise FileNotFoundError(f"{root} does not exist")

    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(sorted(root.rglob(pattern)))
    seen = set(candidates)

    loaded: list[LoadResult] = []
    failed: list[tuple[Path, str]] = []
    skipped: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            loaded.append(load_vendor_file(path))
        except VendorCsvError as error:
            failed.append((path, str(error)))
        except Exception as error:  # a vendor file can fail in any way at all
            failed.append((path, f"{type(error).__name__}: {error}"))

    others = {path for path in root.rglob("*") if path.is_file() and path not in seen}
    skipped.extend(sorted(others))
    return DirectoryScan(
        root=root, loaded=tuple(loaded), failed=tuple(failed), skipped=tuple(skipped)
    )


def suspected_split_dates(frame: pd.DataFrame, *, threshold: float = 0.20) -> list[dt.date]:
    """Sessions where the close-to-close move looks like an unadjusted action.

    A 20% overnight gap in a Nifty 50 constituent is possible but rare; a
    1:2 split shows up as -50%, a 1:1 bonus as -50%, a 1:5 split as -80%.
    Reported as candidates for the corporate-actions file, not as fact.
    """
    if len(frame) < 2:
        return []
    closes = frame["close"]
    dates = pd.DatetimeIndex(frame.index).date
    session_close = closes.groupby(dates).last()
    ratio: pd.Series = session_close / session_close.shift(1)
    moves = pd.Series(np.log(ratio.to_numpy(dtype="float64")), index=session_close.index)
    flagged = moves[moves.abs() > threshold]
    return list(flagged.index)
