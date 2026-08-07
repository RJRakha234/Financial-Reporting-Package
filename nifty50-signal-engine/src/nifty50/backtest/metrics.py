"""Performance metrics.

Every ratio here is reported alongside the raw quantities it came from, because
a Sharpe on its own hides the two things that decide whether it means anything:
how many trades produced it, and how much was paid in costs to get it.

Annualisation is explicit rather than inferred. A metric computed on 15-minute
bars and annualised with 252 is wrong by a factor of five; the caller must say
how many periods make a year.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Below this many trades, per-trade statistics are noise dressed as evidence.
MIN_TRADES_FOR_CONFIDENCE: int = 30

# Returns are fractions, so a standard deviation this small is numerical noise
# rather than a real dispersion. Comparing to exactly 0.0 is not enough: a
# constant series comes back with a std around 1e-19, which sails through an
# equality check and produces a Sharpe of 1e16 — a number that looks like a
# spectacular strategy and is actually a division by nothing.
_MIN_MEANINGFUL_DEVIATION: float = 1e-12


@dataclass(frozen=True, slots=True)
class DrawdownResult:
    max_drawdown: float
    peak_date: pd.Timestamp | None
    trough_date: pd.Timestamp | None
    recovery_date: pd.Timestamp | None
    duration_days: int | None

    @property
    def recovered(self) -> bool:
        return self.recovery_date is not None


@dataclass(slots=True)
class PerformanceReport:
    """The full result set. ``gross`` and ``net`` are always carried together."""

    total_return: float = 0.0
    annualised_return: float = 0.0
    sharpe: float = float("nan")
    sortino: float = float("nan")
    calmar: float = float("nan")
    max_drawdown: float = 0.0
    max_drawdown_duration_days: int | None = None
    win_rate: float = float("nan")
    profit_factor: float = float("nan")
    average_win: float = 0.0
    average_loss: float = 0.0
    expectancy: float = 0.0
    trade_count: int = 0
    exposure_time: float = 0.0
    turnover: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_costs: float = 0.0
    costs_as_pct_of_gross: float = float("nan")
    notes: list[str] = field(default_factory=list)

    @property
    def costs_ate_the_edge(self) -> bool:
        """True when the strategy is profitable gross and unprofitable net.

        The most important single fact about an Indian intraday backtest, and
        the one a summary table is most likely to bury.
        """
        return self.gross_pnl > 0 and self.net_pnl <= 0

    def describe(self) -> str:
        lines = [
            f"  Trades              {self.trade_count:>12,d}",
            f"  Gross PnL           {self.gross_pnl:>12,.2f}",
            f"  Costs               {self.total_costs:>12,.2f}",
            f"  Net PnL             {self.net_pnl:>12,.2f}",
            f"  Costs / gross       {self.costs_as_pct_of_gross:>12.1%}",
            f"  Total return        {self.total_return:>12.2%}",
            f"  Annualised          {self.annualised_return:>12.2%}",
            f"  Sharpe              {self.sharpe:>12.2f}",
            f"  Sortino             {self.sortino:>12.2f}",
            f"  Calmar              {self.calmar:>12.2f}",
            f"  Max drawdown        {self.max_drawdown:>12.2%}",
            f"  Win rate            {self.win_rate:>12.1%}",
            f"  Profit factor       {self.profit_factor:>12.2f}",
            f"  Expectancy/trade    {self.expectancy:>12.2f}",
            f"  Exposure            {self.exposure_time:>12.1%}",
        ]
        if self.costs_ate_the_edge:
            lines.append("  ** PROFITABLE GROSS, LOSS-MAKING NET — the costs ate the edge **")
        lines.extend(f"  NOTE: {note}" for note in self.notes)
        return "\n".join(lines)


def max_drawdown(equity: pd.Series) -> DrawdownResult:
    """Deepest peak-to-trough decline, with its dates and duration."""
    if equity.empty:
        return DrawdownResult(0.0, None, None, None, None)
    running_peak = equity.cummax()
    drawdown = equity / running_peak - 1.0
    trough_date = drawdown.idxmin()
    worst = float(drawdown.min())
    if worst == 0.0:
        return DrawdownResult(0.0, None, None, None, None)

    before_trough = equity.loc[:trough_date]
    peak_date = before_trough.idxmax()
    peak_value = float(equity.loc[peak_date])

    after = equity.loc[trough_date:]
    recovered = after[after >= peak_value]
    recovery_date = recovered.index[0] if len(recovered) else None
    end = recovery_date if recovery_date is not None else equity.index[-1]
    duration = int((pd.Timestamp(end) - pd.Timestamp(peak_date)).days)

    return DrawdownResult(
        max_drawdown=worst,
        peak_date=pd.Timestamp(peak_date),
        trough_date=pd.Timestamp(trough_date),
        recovery_date=pd.Timestamp(recovery_date) if recovery_date is not None else None,
        duration_days=duration,
    )


def sharpe_ratio(
    returns: pd.Series, *, periods_per_year: float, risk_free_rate: float = 0.0
) -> float:
    """Annualised Sharpe.

    ``risk_free_rate`` is an annual rate and defaults to zero. For Indian
    equities a zero risk-free assumption flatters the ratio — the overnight rate
    has been 4-7% over the sample — so pass the real one when it matters.
    """
    clean = returns.dropna()
    if len(clean) < 2:
        return float("nan")
    excess = clean - risk_free_rate / periods_per_year
    deviation = float(excess.std(ddof=1))
    if not np.isfinite(deviation) or deviation < _MIN_MEANINGFUL_DEVIATION:
        return float("nan")
    return float(excess.mean() / deviation * np.sqrt(periods_per_year))


def sortino_ratio(
    returns: pd.Series, *, periods_per_year: float, risk_free_rate: float = 0.0
) -> float:
    """Like Sharpe but penalising downside deviation only."""
    clean = returns.dropna()
    if len(clean) < 2:
        return float("nan")
    excess = clean - risk_free_rate / periods_per_year
    downside = excess[excess < 0]
    if len(downside) < 2:
        return float("nan")
    # Downside deviation is measured about zero, not about the mean: the target
    # is "did not lose money", not "did worse than average".
    deviation = float(np.sqrt((downside**2).mean()))
    if not np.isfinite(deviation) or deviation < _MIN_MEANINGFUL_DEVIATION:
        return float("nan")
    return float(excess.mean() / deviation * np.sqrt(periods_per_year))


def calmar_ratio(annualised_return: float, drawdown: float) -> float:
    """Annualised return divided by the depth of the worst drawdown."""
    if drawdown == 0.0:
        return float("nan")
    return annualised_return / abs(drawdown)


def annualised_return(equity: pd.Series, *, periods_per_year: float) -> float:
    """Compound annual growth rate implied by the equity curve."""
    clean = equity.dropna()
    if len(clean) < 2 or float(clean.iloc[0]) <= 0:
        return float("nan")
    total_growth = float(clean.iloc[-1]) / float(clean.iloc[0])
    if total_growth <= 0:
        return -1.0
    years = (len(clean) - 1) / periods_per_year
    if years <= 0:
        return float("nan")
    return float(total_growth ** (1.0 / years) - 1.0)


def deflated_sharpe(observed_sharpe: float, *, trials: int, sample_size: int) -> float:
    """Sharpe adjusted for the number of configurations that were tried.

    Testing many parameter sets guarantees that the best one looks good. This
    subtracts the Sharpe you would expect the *best of N random* strategies to
    show, so a strategy chosen from a wide sweep has to clear a higher bar.
    Roughly follows Bailey & López de Prado; the point is the direction and
    magnitude of the haircut, not a precise p-value.
    """
    if trials < 1 or sample_size < 2 or not np.isfinite(observed_sharpe):
        return float("nan")
    if trials == 1:
        return observed_sharpe
    euler_mascheroni = 0.5772156649
    # Expected maximum of `trials` draws from a standard normal.
    expected_max = (1 - euler_mascheroni) * _inverse_normal_cdf(
        1 - 1.0 / trials
    ) + euler_mascheroni * _inverse_normal_cdf(1 - 1.0 / (trials * np.e))
    haircut = expected_max / np.sqrt(sample_size)
    return float(observed_sharpe - haircut)


def _inverse_normal_cdf(probability: float) -> float:
    """Acklam's rational approximation to the standard normal quantile."""
    if not 0.0 < probability < 1.0:
        raise ValueError("probability must be in (0, 1)")
    a = [
        -39.6968302866538,
        220.946098424521,
        -275.928510446969,
        138.357751867269,
        -30.6647980661472,
        2.50662827745924,
    ]
    b = [
        -54.4760987982241,
        161.585836858041,
        -155.698979859887,
        66.8013118877197,
        -13.2806815528857,
    ]
    c = [
        -0.00778489400243029,
        -0.322396458041136,
        -2.40075827716184,
        -2.54973253934373,
        4.37466414146497,
        2.93816398269878,
    ]
    d = [0.00778469570904146, 0.32246712907004, 2.445134137143, 3.75440866190742]
    low, high = 0.02425, 1 - 0.02425
    if probability < low:
        q = np.sqrt(-2 * np.log(probability))
        return float(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    if probability > high:
        q = np.sqrt(-2 * np.log(1 - probability))
        return -float(
            (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
            / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
        )
    q = probability - 0.5
    r = q * q
    return float(
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


def monte_carlo_drawdown(
    trade_pnls: list[float],
    *,
    starting_equity: float,
    simulations: int = 1000,
    seed: int = 0,
) -> dict[str, float]:
    """Drawdown distribution under reshuffled trade order.

    The realised sequence is one draw from many possible orderings. Reshuffling
    keeps the same trades and the same total PnL but answers "how bad could the
    path have been" — which is the question that decides whether the strategy is
    survivable, not merely profitable.
    """
    if not trade_pnls:
        return {}
    rng = np.random.default_rng(seed)
    pnls = np.array(trade_pnls, dtype="float64")
    worst = np.empty(simulations, dtype="float64")
    for i in range(simulations):
        shuffled = rng.permutation(pnls)
        equity = starting_equity + np.cumsum(shuffled)
        peak = np.maximum.accumulate(equity)
        worst[i] = float((equity / peak - 1.0).min())
    return {
        "median": float(np.median(worst)),
        "p95": float(np.percentile(worst, 5)),  # 5th pct of a negative series
        "p99": float(np.percentile(worst, 1)),
        "worst": float(worst.min()),
    }


def summarise(
    equity: pd.Series,
    trade_pnls: list[float],
    *,
    periods_per_year: float,
    gross_pnl: float,
    total_costs: float,
    exposure_time: float,
    turnover: float,
    risk_free_rate: float = 0.0,
) -> PerformanceReport:
    """Build the full report from an equity curve and a list of trade results."""
    report = PerformanceReport()
    if equity.empty:
        report.notes.append("no equity curve: the strategy never traded")
        return report

    returns = equity.pct_change(fill_method=None).dropna()
    drawdown = max_drawdown(equity)

    report.total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    report.annualised_return = annualised_return(equity, periods_per_year=periods_per_year)
    report.sharpe = sharpe_ratio(
        returns, periods_per_year=periods_per_year, risk_free_rate=risk_free_rate
    )
    report.sortino = sortino_ratio(
        returns, periods_per_year=periods_per_year, risk_free_rate=risk_free_rate
    )
    report.max_drawdown = drawdown.max_drawdown
    report.max_drawdown_duration_days = drawdown.duration_days
    report.calmar = calmar_ratio(report.annualised_return, drawdown.max_drawdown)

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    report.trade_count = len(trade_pnls)
    report.win_rate = len(wins) / len(trade_pnls) if trade_pnls else float("nan")
    report.average_win = float(np.mean(wins)) if wins else 0.0
    report.average_loss = float(np.mean(losses)) if losses else 0.0
    gross_wins, gross_losses = sum(wins), abs(sum(losses))
    report.profit_factor = (
        gross_wins / gross_losses
        if gross_losses > 0
        else float("inf")
        if gross_wins
        else float("nan")
    )
    report.expectancy = float(np.mean(trade_pnls)) if trade_pnls else 0.0

    report.gross_pnl = gross_pnl
    report.total_costs = total_costs
    report.net_pnl = gross_pnl - total_costs
    report.costs_as_pct_of_gross = total_costs / abs(gross_pnl) if gross_pnl != 0 else float("nan")
    report.exposure_time = exposure_time
    report.turnover = turnover

    if report.trade_count < MIN_TRADES_FOR_CONFIDENCE:
        report.notes.append(
            f"only {report.trade_count} trades — per-trade statistics are not "
            f"meaningful below about {MIN_TRADES_FOR_CONFIDENCE}"
        )
    if report.costs_ate_the_edge:
        report.notes.append("gross profit became a net loss after the cost stack")
    return report
