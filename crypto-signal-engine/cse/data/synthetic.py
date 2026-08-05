"""Deterministic synthetic market generator — offline development and testing.

READ THIS BEFORE TRUSTING ANY NUMBER PRODUCED FROM SYNTHETIC DATA
-----------------------------------------------------------------
This generator exists because the build environment has no network route to
Binance. It produces a *plausible-looking* series, not a real one. Its price
process is a regime-switching geometric Brownian motion with a common market
factor: it has volatility clustering and cross-asset correlation, and it has
none of the things that actually decide whether a strategy works — order-flow
reflexivity, liquidation cascades, funding-rate feedback, news, or the fat
tails and microstructure of a real book.

Consequently:

* Backtest numbers computed on synthetic data measure whether the **plumbing**
  is correct (no look-ahead, costs applied, positions tracked). They are
  **zero evidence** about whether the strategy makes money.
* Any report generated from this source is stamped ``data_source: synthetic``
  so a result can never be mistaken for a real one.

The generator is seeded, so output is byte-identical across runs. That is what
makes the pipeline tests reproducible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from cse.config import SyntheticConfig
from cse.data.schema import (
    CANDLE_COLUMNS,
    coerce_dtypes,
    floor_to_interval,
)

MS_PER_MINUTE = 60_000
GENERATION_INTERVAL_MS = MS_PER_MINUTE  # generate at 1m, aggregate upward


class SyntheticMarket:
    """Generates internally consistent candles for a set of correlated symbols.

    Consistency across timeframes is structural rather than approximate: a
    single 1-minute path is generated per symbol and every higher timeframe is
    an exact aggregation of it. A 4h bar therefore contains precisely the 240
    1m bars it should, which is what makes the multi-timeframe features in
    Phase 2 testable.
    """

    def __init__(self, config: SyntheticConfig, symbols: list[str]) -> None:
        self._config = config
        self._symbols = list(symbols)
        if config.market_factor_symbol not in self._symbols:
            # The factor still drives the alts even if it is not itself requested.
            self._symbols = [config.market_factor_symbol, *self._symbols]
        missing = [s for s in self._symbols if s not in config.initial_price]
        if missing:
            raise KeyError(f"synthetic.initial_price missing entries for {missing}")

    @property
    def start_ms(self) -> int:
        return int(pd.Timestamp(self._config.start).value // 1_000_000)

    def _per_bar_params(self) -> tuple[float, float]:
        """Per-1m-bar drift and volatility implied by the annualized settings."""
        bars_per_year = float(self._config.bars_per_year)
        drift = self._config.annual_drift / bars_per_year
        vol = self._config.annual_volatility / np.sqrt(bars_per_year)
        return drift, vol

    def _regime_multipliers(self, n_bars: int, rng: np.random.Generator) -> np.ndarray:
        """A piecewise-constant volatility multiplier path (Markov switching)."""
        multipliers = np.asarray(self._config.regime_vol_multipliers, dtype=np.float64)
        switches = rng.random(n_bars) < self._config.regime_switch_probability
        # Each switch picks a new regime index; between switches the regime holds.
        draws = rng.integers(0, len(multipliers), size=n_bars)
        regime_idx = np.empty(n_bars, dtype=np.int64)
        current = int(draws[0])
        for i in range(n_bars):
            if switches[i]:
                current = int(draws[i])
            regime_idx[i] = current
        return multipliers[regime_idx]

    def generate_1m(self, start_ms: int, end_ms: int) -> dict[str, pd.DataFrame]:
        """Generate 1-minute candles for every symbol over ``[start_ms, end_ms]``.

        Bounds are inclusive on ``open_time`` and snapped to the minute grid.
        """
        start = floor_to_interval(start_ms, GENERATION_INTERVAL_MS)
        end = floor_to_interval(end_ms, GENERATION_INTERVAL_MS)
        if end < start:
            raise ValueError(f"end_ms {end_ms} precedes start_ms {start_ms}")
        n_bars = int((end - start) // GENERATION_INTERVAL_MS) + 1

        drift, vol = self._per_bar_params()
        factor_symbol = self._config.market_factor_symbol

        # The market factor gets the base seed; each alt gets its own stream so
        # adding or removing a symbol does not perturb the others' paths.
        factor_rng = np.random.default_rng(self._config.seed)
        regime = self._regime_multipliers(n_bars, factor_rng)
        factor_shocks = factor_rng.standard_normal(n_bars) * vol * regime

        frames: dict[str, pd.DataFrame] = {}
        for symbol in self._symbols:
            symbol_seed = self._config.seed + (abs(hash(symbol)) % 1_000_003)
            rng = np.random.default_rng(symbol_seed)

            if symbol == factor_symbol:
                log_returns = drift + factor_shocks
            else:
                beta = float(self._config.beta.get(symbol, 1.0))
                idio_fraction = self._config.idiosyncratic_vol_fraction
                # Split total variance between the factor component and the
                # symbol's own noise, so beta is the systematic loading and the
                # remainder is genuinely idiosyncratic.
                systematic = beta * factor_shocks * np.sqrt(1.0 - idio_fraction)
                idiosyncratic = (
                    rng.standard_normal(n_bars) * vol * regime * beta * np.sqrt(idio_fraction)
                )
                log_returns = drift + systematic + idiosyncratic

            frames[symbol] = self._path_to_candles(
                symbol=symbol,
                start_ms=start,
                log_returns=log_returns,
                regime=regime,
                rng=rng,
            )
        return {s: frames[s] for s in frames if s in self._symbols}

    def _path_to_candles(
        self,
        *,
        symbol: str,
        start_ms: int,
        log_returns: np.ndarray,
        regime: np.ndarray,
        rng: np.random.Generator,
    ) -> pd.DataFrame:
        """Turn a per-bar log-return path into OHLCV with realistic wicks."""
        n_bars = len(log_returns)
        substeps = self._config.intrabar_substeps

        closes = float(self._config.initial_price[symbol]) * np.exp(np.cumsum(log_returns))
        opens = np.empty(n_bars, dtype=np.float64)
        opens[0] = float(self._config.initial_price[symbol])
        opens[1:] = closes[:-1]

        # Within each bar, walk a Brownian bridge from open to close in
        # `substeps` increments and take the realised extremes. This produces
        # wicks whose size scales with the bar's volatility, instead of the
        # uniform-noise wicks a naive generator emits.
        bridge = rng.standard_normal((n_bars, substeps))
        bridge -= bridge.mean(axis=1, keepdims=True)
        step_scale = (np.abs(log_returns) + regime * 1e-4)[:, None]
        cumulative = np.cumsum(bridge * step_scale, axis=1)
        log_open = np.log(opens)[:, None]
        log_close = np.log(closes)
        ramp = np.linspace(0.0, 1.0, substeps)[None, :]
        path = log_open + (log_close - np.log(opens))[:, None] * ramp + cumulative
        prices = np.exp(path)

        highs = np.maximum(np.maximum(opens, closes), prices.max(axis=1))
        lows = np.minimum(np.minimum(opens, closes), prices.min(axis=1))

        # Volume rises with absolute return (a real and well-documented effect)
        # plus lognormal noise.
        abs_return = np.abs(log_returns)
        scale = 1.0 + self._config.volume_volatility_elasticity * (
            abs_return / (abs_return.mean() + 1e-12)
        )
        noise = np.exp(rng.standard_normal(n_bars) * self._config.volume_noise)
        volume = self._config.base_volume * scale * noise

        # Aggressor imbalance leans with the bar's direction.
        direction = np.sign(log_returns)
        ratio = np.clip(
            self._config.taker_buy_ratio_mean
            + direction * np.abs(rng.standard_normal(n_bars)) * self._config.taker_buy_ratio_std,
            0.01,
            0.99,
        )

        open_time = start_ms + np.arange(n_bars, dtype=np.int64) * GENERATION_INTERVAL_MS
        typical_price = (highs + lows + closes) / 3.0
        quote_volume = volume * typical_price

        frame = pd.DataFrame(
            {
                "open_time": open_time,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volume,
                "close_time": open_time + GENERATION_INTERVAL_MS - 1,
                "quote_volume": quote_volume,
                "trades": np.maximum(1, (volume * 0.5).astype(np.int64)),
                "taker_buy_base": volume * ratio,
                "taker_buy_quote": quote_volume * ratio,
            }
        )
        return coerce_dtypes(frame)


def aggregate_candles(frame: pd.DataFrame, target_interval_ms: int) -> pd.DataFrame:
    """Aggregate fine-grained candles to a coarser timeframe.

    Standard OHLCV aggregation: first open, max high, min low, last close, and
    summed volumes.

    Only *complete* target bars are emitted. A bucket is complete when it holds
    exactly ``target_interval_ms / source_interval_ms`` source bars, so partial
    buckets at **either end** are dropped: a trailing one because emitting it is
    the unclosed-candle bug in another costume, and a leading one because an
    input that starts mid-bucket would otherwise produce a bar whose open, high,
    low and volume silently omit the earlier part of the period.
    """
    if frame.empty:
        return frame.copy()

    working = coerce_dtypes(frame)
    source_interval = _infer_interval_ms(working)
    if target_interval_ms < source_interval:
        raise ValueError(
            f"cannot aggregate {source_interval}ms bars up to {target_interval_ms}ms (target is finer)"
        )
    if target_interval_ms % source_interval != 0:
        raise ValueError(
            f"target interval {target_interval_ms}ms is not a multiple of source {source_interval}ms"
        )
    expected_per_bucket = target_interval_ms // source_interval

    bucket = (working["open_time"] // target_interval_ms) * target_interval_ms
    grouped = working.groupby(bucket, sort=True)

    aggregated = pd.DataFrame(
        {
            "open_time": grouped["open_time"].min(),
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
            "close_time": grouped["open_time"].min() + target_interval_ms - 1,
            "quote_volume": grouped["quote_volume"].sum(),
            "trades": grouped["trades"].sum(),
            "taker_buy_base": grouped["taker_buy_base"].sum(),
            "taker_buy_quote": grouped["taker_buy_quote"].sum(),
        }
    ).reset_index(drop=True)

    counts = grouped.size().reset_index(drop=True)
    complete = counts == expected_per_bucket
    aggregated = aggregated.loc[complete].reset_index(drop=True)

    return coerce_dtypes(aggregated.loc[:, list(CANDLE_COLUMNS)])


def _infer_interval_ms(frame: pd.DataFrame) -> int:
    """Modal spacing between consecutive bars."""
    if len(frame) < 2:
        return GENERATION_INTERVAL_MS
    deltas = np.diff(frame["open_time"].to_numpy(dtype=np.int64))
    positive = deltas[deltas > 0]
    if positive.size == 0:
        return GENERATION_INTERVAL_MS
    values, counts = np.unique(positive, return_counts=True)
    return int(values[int(np.argmax(counts))])
