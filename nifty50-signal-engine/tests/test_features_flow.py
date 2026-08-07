"""India-specific flow features, with the publication-lag rules under test.

The tests that matter here are the boring-looking alignment ones. Getting the
lag wrong on delivery percentage does not produce an error, a warning, or an
implausible number — it produces a backtest that works beautifully and a live
signal that does not, and the gap between them is six months of wondering why.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from nifty50.config import Config
from nifty50.data.synthetic import generate_session_bars
from nifty50.domain import Timeframe
from nifty50.features.flow import (
    LookAheadError,
    align_daily_to_bars,
    circuit_band_state,
    delivery_features,
    fno_ban_flag,
    index_event_proximity,
    participant_flow_features,
)
from nifty50.features.session import session_date
from nifty50.trading_calendar import TradingCalendar

WEEK_START = dt.date(2025, 8, 4)  # Monday
WEEK_END = dt.date(2025, 8, 8)  # Friday


@pytest.fixture
def bars(calendar: TradingCalendar) -> pd.DataFrame:
    return generate_session_bars(calendar, WEEK_START, WEEK_END, Timeframe.M15, seed=42)


@pytest.fixture
def sessions(bars: pd.DataFrame) -> list[dt.date]:
    return sorted(set(session_date(bars)))


def daily(sessions: list[dt.date], values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.Index(sessions, name="date"), name="daily")


class TestAlignment:
    def test_one_session_of_lag_shows_the_previous_session_value(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        source = daily(sessions, [10.0, 20.0, 30.0, 40.0, 50.0])
        aligned = align_daily_to_bars(source, bars, calendar, lag_sessions=1)

        by_session = aligned.groupby(session_date(bars)).first()
        # Tuesday's bars carry Monday's number, and so on down the week.
        assert by_session.loc[sessions[1]] == 10.0  # Monday's value, not Tuesday's
        assert by_session.loc[sessions[2]] == 20.0
        assert by_session.loc[sessions[4]] == 40.0
        # Monday itself has no prior session inside the sample.
        assert np.isnan(by_session.loc[sessions[0]])

    def test_the_value_is_constant_across_a_session(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        source = daily(sessions, [10.0, 20.0, 30.0, 40.0, 50.0])
        aligned = align_daily_to_bars(source, bars, calendar, lag_sessions=1)
        # A daily number is known at the open and does not change intraday.
        assert aligned.groupby(session_date(bars)).nunique().max() <= 1

    def test_zero_lag_is_allowed_but_means_something_different(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        source = daily(sessions, [10.0, 20.0, 30.0, 40.0, 50.0])
        aligned = align_daily_to_bars(source, bars, calendar, lag_sessions=0)
        by_session = aligned.groupby(session_date(bars)).first()
        assert by_session.loc[sessions[2]] == 30.0

    def test_a_negative_lag_is_refused_rather_than_clamped(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        source = daily(sessions, [10.0, 20.0, 30.0, 40.0, 50.0])
        with pytest.raises(LookAheadError, match="published after"):
            align_daily_to_bars(source, bars, calendar, lag_sessions=-1)

    def test_lag_walks_the_trading_calendar_not_the_wall_calendar(
        self, calendar: TradingCalendar
    ) -> None:
        # 2025-08-15 is Independence Day. The session after it is Monday the
        # 18th, whose lagged value must be Thursday the 14th's — not the
        # holiday's, and not Friday-as-if-it-traded.
        bars = generate_session_bars(
            calendar, dt.date(2025, 8, 13), dt.date(2025, 8, 19), Timeframe.M15, seed=1
        )
        present = sorted(set(session_date(bars)))
        assert dt.date(2025, 8, 15) not in present

        source = daily(present, [float(index) for index in range(len(present))])
        aligned = align_daily_to_bars(source, bars, calendar, lag_sessions=1)
        by_session = aligned.groupby(session_date(bars)).first()

        monday = dt.date(2025, 8, 18)
        thursday = dt.date(2025, 8, 14)
        assert by_session.loc[monday] == source.loc[thursday]


class TestDelivery:
    def test_delivery_columns_are_lagged_by_default(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        source = daily(sessions, [40.0, 45.0, 50.0, 55.0, 60.0])
        frame = delivery_features(source, bars, calendar, window=2)
        by_session = frame["delivery_pct"].groupby(session_date(bars)).first()
        # Wednesday sees Tuesday's 45, never Wednesday's own 50.
        assert by_session.loc[sessions[2]] == 45.0
        assert by_session.loc[sessions[2]] != 50.0

    def test_a_window_of_one_session_is_refused(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        source = daily(sessions, [40.0, 45.0, 50.0, 55.0, 60.0])
        with pytest.raises(ValueError, match="two sessions"):
            delivery_features(source, bars, calendar, window=1)


class TestParticipantFlows:
    def test_opposed_flows_are_flagged(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        fii = daily(sessions, [-1000.0, -800.0, 500.0, 200.0, -300.0])
        dii = daily(sessions, [900.0, 700.0, 400.0, -100.0, 250.0])
        frame = participant_flow_features(fii, dii, bars, calendar, window=2)
        opposed = frame["fii_dii_opposed"].groupby(session_date(bars)).first()

        # Tuesday's bars carry Monday's data: FII -1000 against DII +900.
        assert opposed.loc[sessions[1]] == 1.0
        # Thursday carries Wednesday: both positive, not opposed.
        assert opposed.loc[sessions[3]] == 0.0

    def test_net_institutional_is_the_sum(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        fii = daily(sessions, [-1000.0, -800.0, 500.0, 200.0, -300.0])
        dii = daily(sessions, [900.0, 700.0, 400.0, -100.0, 250.0])
        frame = participant_flow_features(fii, dii, bars, calendar, window=2)
        net = frame["net_institutional_crore"].groupby(session_date(bars)).first()
        assert net.loc[sessions[1]] == pytest.approx(-100.0)


class TestCircuitBands:
    def test_band_position_spans_zero_to_one(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=3, freq="15min", tz="Asia/Kolkata")
        previous = pd.Series([100.0] * 3, index=index)
        close = pd.Series([90.0, 100.0, 110.0], index=index)
        high = close.copy()
        low = close.copy()

        frame = circuit_band_state(high, low, close, previous, band_pct=0.10)
        assert frame["band_position"].tolist() == pytest.approx([0.0, 0.5, 1.0])
        assert frame["at_lower_band"].tolist() == [1.0, 0.0, 0.0]
        assert frame["at_upper_band"].tolist() == [0.0, 0.0, 1.0]

    def test_an_out_of_range_band_is_refused(self) -> None:
        index = pd.date_range("2025-08-04 09:15", periods=1, freq="15min", tz="Asia/Kolkata")
        one = pd.Series([100.0], index=index)
        with pytest.raises(ValueError, match="fraction"):
            circuit_band_state(one, one, one, one, band_pct=10.0)


class TestKnownBeforeTheOpen:
    def test_the_ban_flag_marks_whole_sessions(
        self, bars: pd.DataFrame, sessions: list[dt.date]
    ) -> None:
        flag = fno_ban_flag([sessions[1], sessions[3]], bars)
        by_session = flag.groupby(session_date(bars)).max()
        assert by_session.loc[sessions[1]] == 1.0
        assert by_session.loc[sessions[2]] == 0.0
        assert by_session.loc[sessions[3]] == 1.0
        # The flag is genuinely known at the open, so it applies to every bar.
        assert flag.groupby(session_date(bars)).nunique().max() == 1

    def test_index_event_proximity_peaks_on_the_effective_date(
        self, bars: pd.DataFrame, sessions: list[dt.date]
    ) -> None:
        proximity = index_event_proximity([sessions[4]], bars, lead_sessions=5)
        by_session = proximity.groupby(session_date(bars)).first()
        assert by_session.loc[sessions[4]] == pytest.approx(1.0)
        assert by_session.loc[sessions[0]] < by_session.loc[sessions[3]]

    def test_no_events_means_zero_everywhere_not_nan(self, bars: pd.DataFrame) -> None:
        proximity = index_event_proximity([], bars)
        assert (proximity == 0.0).all()


class TestTheLagRuleIsLoadBearing:
    def test_zero_lag_delivery_would_be_caught_by_the_truncation_test(
        self, bars: pd.DataFrame, sessions: list[dt.date], calendar: TradingCalendar
    ) -> None:
        """A guard on the guard, mirroring the one in ``test_lookahead.py``.

        Delivery percentage is mechanically correlated with the session's own
        move, so a zero-lag alignment hands the model the answer. This asserts
        the mistake is *detectable*: with lag 1 the value at a bar survives
        truncating everything from that session onward, and with lag 0 it does
        not, because the number does not exist yet.
        """
        source = daily(sessions, [40.0, 45.0, 50.0, 55.0, 60.0])
        current = sessions[3]
        # What a trader could actually see partway through `current`: bars up
        # to that point, and daily data only for prior sessions.
        visible_bars = bars[session_date(bars) <= current]
        visible_daily = source[source.index < current]

        lagged_full = align_daily_to_bars(source, visible_bars, calendar, lag_sessions=1)
        lagged_truncated = align_daily_to_bars(
            visible_daily, visible_bars, calendar, lag_sessions=1
        )
        assert lagged_full.iloc[-1] == lagged_truncated.iloc[-1] == 50.0

        unlagged_full = align_daily_to_bars(source, visible_bars, calendar, lag_sessions=0)
        unlagged_truncated = align_daily_to_bars(
            visible_daily, visible_bars, calendar, lag_sessions=0
        )
        assert unlagged_full.iloc[-1] == 55.0  # today's own number
        assert np.isnan(unlagged_truncated.iloc[-1])  # which did not exist yet


class TestPipelineIntegration:
    def test_circuit_bands_never_see_their_own_session_close(
        self, bars: pd.DataFrame, calendar: TradingCalendar, config: Config
    ) -> None:
        from nifty50.features.pipeline import _previous_session_close

        previous = _previous_session_close(bars)
        dates = session_date(bars)
        session_closes = bars["close"].groupby(dates).last()

        for day in sorted(set(dates))[1:]:
            in_session = previous[dates == day]
            assert (in_session == in_session.iloc[0]).all()
            assert in_session.iloc[0] == pytest.approx(
                session_closes.shift(1).loc[day]
            )
            # The value must not be this session's own close.
            assert in_session.iloc[0] != session_closes.loc[day]
