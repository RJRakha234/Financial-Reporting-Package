"""Volume and order-flow features.

Volume z-score, OBV and its slope, VWAP with distance measured in ATR units,
taker aggressor imbalance, order-book imbalance, and volume-profile POC and
value-area edges.

A note on where aggressor flow comes from, because backtest and live differ:

* **Historical** — klines carry ``taker_buy_base``, the aggregate volume bought
  by the aggressing side over the bar. That is exactly the quantity we want,
  already aggregated by the exchange.
* **Live** — the ``aggTrade`` stream gives the same thing trade-by-trade, so
  intrabar resolution is available that history does not have.

The bar-level feature is therefore identical in both; only the intrabar detail
differs. Any feature built on intrabar flow would be untestable against history
and is not computed here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cse.config import VolumeFeatureConfig
from cse.features.base import rolling_slope, rolling_zscore, safe_divide


def on_balance_volume(close: pd.Series[float], volume: pd.Series[float]) -> pd.Series[float]:
    """OBV: running volume signed by the direction of the close."""
    direction = pd.Series(
        np.sign(close.diff().fillna(0.0).to_numpy(dtype=np.float64)), index=close.index
    )
    result = (direction * volume).cumsum()
    result.name = "obv"
    return result


def rolling_vwap(frame: pd.DataFrame, window: int) -> pd.Series[float]:
    """Volume-weighted average price over a trailing window.

    Rolling rather than session-anchored: crypto trades continuously, so there
    is no session boundary to anchor to, and a fixed UTC-day anchor would make
    the feature discontinuous at midnight for no economic reason.
    """
    typical_price = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    weighted = typical_price * frame["volume"]
    numerator = weighted.rolling(window, min_periods=window).sum()
    denominator = frame["volume"].rolling(window, min_periods=window).sum()
    result = safe_divide(numerator, denominator)
    result.name = "vwap"
    return result


def taker_imbalance(frame: pd.DataFrame) -> pd.Series[float]:
    """Buy-vs-sell aggressor imbalance in [-1, 1].

    ``+1`` means every unit traded was bought by an aggressor lifting the offer;
    ``-1`` means all aggressive selling.
    """
    buy_volume = frame["taker_buy_base"]
    sell_volume = frame["volume"] - buy_volume
    result = safe_divide(buy_volume - sell_volume, frame["volume"])
    result.name = "taker_imbalance"
    return result


def order_book_imbalance(bids: list[list[float]], asks: list[list[float]], levels: int) -> float:
    """Top-N depth imbalance in [-1, 1] from a partial order-book snapshot.

    Positive means resting bid size exceeds resting ask size. Computed from the
    live ``depth20@100ms`` stream; it has no historical counterpart, so it is
    available live but is absent from any backtest — stated plainly rather than
    back-filled with a proxy that would make the backtest look better than the
    live system can be.
    """
    bid_size = sum(quantity for _, quantity in bids[:levels])
    ask_size = sum(quantity for _, quantity in asks[:levels])
    total = bid_size + ask_size
    if total <= 0.0:
        return float("nan")
    return float((bid_size - ask_size) / total)


def volume_profile(
    frame: pd.DataFrame, window: int, bins: int, value_area_fraction: float
) -> pd.DataFrame:
    """Rolling volume profile: point of control and value-area edges.

    For each bar, the trailing ``window`` bars' volume is histogrammed by price
    (each bar's volume assigned to its typical price). The POC is the busiest
    price bucket; the value area is the narrowest contiguous band around it
    holding ``value_area_fraction`` of the volume.
    """
    typical_price = ((frame["high"] + frame["low"] + frame["close"]) / 3.0).to_numpy(
        dtype=np.float64
    )
    volume = frame["volume"].to_numpy(dtype=np.float64)
    n = len(frame)

    poc = np.full(n, np.nan, dtype=np.float64)
    value_high = np.full(n, np.nan, dtype=np.float64)
    value_low = np.full(n, np.nan, dtype=np.float64)

    for end in range(window - 1, n):
        start = end - window + 1
        prices = typical_price[start : end + 1]
        volumes = volume[start : end + 1]
        if np.isnan(prices).any() or np.isnan(volumes).any():
            continue
        low, high = prices.min(), prices.max()
        if not np.isfinite(low) or not np.isfinite(high) or high <= low:
            poc[end] = float(low)
            value_low[end] = float(low)
            value_high[end] = float(high)
            continue

        edges = np.linspace(low, high, bins + 1)
        histogram, _ = np.histogram(prices, bins=edges, weights=volumes)
        centres = (edges[:-1] + edges[1:]) / 2.0

        peak = int(np.argmax(histogram))
        poc[end] = float(centres[peak])

        # Grow outward from the POC, always taking the heavier neighbour, until
        # the target share of volume is enclosed.
        target = value_area_fraction * histogram.sum()
        included = histogram[peak]
        lower_index = upper_index = peak
        while included < target and (lower_index > 0 or upper_index < bins - 1):
            below = histogram[lower_index - 1] if lower_index > 0 else -1.0
            above = histogram[upper_index + 1] if upper_index < bins - 1 else -1.0
            if above >= below:
                upper_index += 1
                included += histogram[upper_index]
            else:
                lower_index -= 1
                included += histogram[lower_index]
        value_low[end] = float(edges[lower_index])
        value_high[end] = float(edges[upper_index + 1])

    return pd.DataFrame(
        {
            "volume_poc": poc,
            "value_area_high": value_high,
            "value_area_low": value_low,
        },
        index=frame.index,
    )


def compute(
    frame: pd.DataFrame, config: VolumeFeatureConfig, atr_values: pd.Series[float] | None = None
) -> pd.DataFrame:
    """All volume/flow features for a canonical candle frame.

    ``atr_values`` is optional; when supplied, distance-from-VWAP is additionally
    expressed in ATR units, which is what makes it comparable across symbols and
    volatility regimes.
    """
    close = frame["close"]
    out = pd.DataFrame(index=frame.index)

    out["volume_zscore"] = rolling_zscore(frame["volume"], config.zscore_window)

    obv = on_balance_volume(close, frame["volume"])
    out["obv"] = obv
    out["obv_slope"] = rolling_slope(obv, config.obv_slope_window)

    vwap = rolling_vwap(frame, config.vwap_window)
    out["vwap"] = vwap
    out["vwap_distance"] = close - vwap
    out["vwap_distance_pct"] = safe_divide(close - vwap, vwap)
    if atr_values is not None:
        out["vwap_distance_atr"] = safe_divide(close - vwap, atr_values)

    out["taker_imbalance"] = taker_imbalance(frame)
    out["taker_imbalance_zscore"] = rolling_zscore(out["taker_imbalance"], config.zscore_window)

    profile = volume_profile(
        frame, config.profile_window, config.profile_bins, config.value_area_fraction
    )
    out = pd.concat([out, profile], axis=1)
    out["price_vs_poc"] = safe_divide(close - profile["volume_poc"], profile["volume_poc"])
    inside = (close >= profile["value_area_low"]) & (close <= profile["value_area_high"])
    out["inside_value_area"] = inside.astype("int64")
    out.loc[profile["value_area_low"].isna(), "inside_value_area"] = 0
    return out
