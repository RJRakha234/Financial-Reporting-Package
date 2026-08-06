"""Helpers for the OHLCV frame contract.

``DataFrame.index`` is statically an ``Index[Any]``, so every place that relies
on it being a tz-aware :class:`~pandas.DatetimeIndex` is a place where the
contract is assumed rather than checked. :func:`bar_index` turns that assumption
into a single, typed, checked call — which is both what the type checker wants
and what catches a UTC-naive frame the moment it enters a function.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from nifty50.domain import BAR_INDEX_NAME, IST


def bar_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    """Return ``frame``'s index, enforcing the tz-aware bar-index contract."""
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"bar frame must be indexed by a DatetimeIndex, got {type(index).__name__}")
    if index.tz is None:
        raise ValueError("bar frame index must be tz-aware; naive timestamps are rejected")
    return index


def ist_index(frame: pd.DataFrame) -> pd.DatetimeIndex:
    """The bar index converted to IST, ready for ``.date`` extraction."""
    return bar_index(frame).tz_convert(IST)


def empty_bars(columns: tuple[str, ...]) -> pd.DataFrame:
    """An empty OHLCV frame that still satisfies the index contract."""
    frame = pd.DataFrame(columns=list(columns))
    frame.index = pd.DatetimeIndex([], tz=IST, name=BAR_INDEX_NAME)
    return frame


def as_float(value: Any) -> float:
    """Coerce a pandas scalar to ``float``.

    Scalar access through ``.at``/``.iat`` is typed as a wide union, and a
    ``float()`` call on it does not type-check even when the column is numeric.
    """
    return float(value)
