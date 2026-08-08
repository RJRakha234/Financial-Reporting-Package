"""The dashboard: state, routes, and the two things it must never do.

It must never bind anywhere but localhost — the payload contains capital and
position sizing and there is no authentication in front of it. And it must
never present an unclosed bar as a finished one, because that is what makes a
live chart disagree with the backtest computed from the same stream.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.request
from typing import Any

import pytest

from nifty50.dashboard.server import DashboardBindError, build_server
from nifty50.dashboard.state import DashboardState, RiskContext
from nifty50.data.stream import EngineState, StreamHealth
from nifty50.domain import IST, Candle, Timeframe


def candle(
    minute: int, *, closed: bool = True, price: float = 100.0, partial: bool = False
) -> Candle:
    return Candle(
        instrument_key="RELIANCE",
        timeframe=Timeframe.M15,
        ts=dt.datetime(2026, 8, 7, 9, 15, tzinfo=IST) + dt.timedelta(minutes=15 * minute),
        open=price, high=price + 1.0, low=price - 1.0, close=price,
        volume=1000, closed=closed, partial=partial,
    )


def risk() -> RiskContext:
    return RiskContext(
        capital_inr=500_000.0, risk_fraction=0.01, breakeven_pct=0.00083, atr_pct=0.003
    )


class TestState:
    def test_closed_and_forming_bars_are_kept_apart(self) -> None:
        """The load-bearing distinction.

        A 15-minute bar stamped 10:15 is not finished until 10:30. Charting it
        as a completed candle makes the last bar on every chart a value that
        redraws itself, and it is what makes a live chart disagree with a
        backtest built from the same stream.
        """
        state = DashboardState(risk=risk())
        state.on_candle(candle(0))
        state.on_candle(candle(1, closed=False, price=105.0))

        payload = state.series_json("RELIANCE", Timeframe.M15)
        assert payload is not None
        assert len(payload["closed"]) == 1
        assert payload["forming"]["close"] == 105.0

    def test_a_forming_bar_is_replaced_when_it_closes(self) -> None:
        state = DashboardState(risk=risk())
        state.on_candle(candle(1, closed=False, price=105.0))
        state.on_candle(candle(1, closed=True, price=106.0))
        payload = state.series_json("RELIANCE", Timeframe.M15)
        assert payload is not None
        assert payload["forming"] is None
        assert len(payload["closed"]) == 1
        assert payload["closed"][0]["close"] == 106.0

    def test_a_re_emitted_bar_replaces_rather_than_duplicates(self) -> None:
        # The aggregator can flush the same interval twice around a reconnect.
        state = DashboardState(risk=risk())
        state.on_candle(candle(0, price=100.0))
        state.on_candle(candle(0, price=101.0))
        payload = state.series_json("RELIANCE", Timeframe.M15)
        assert payload is not None
        assert len(payload["closed"]) == 1
        assert payload["closed"][0]["close"] == 101.0

    def test_memory_is_bounded(self) -> None:
        state = DashboardState(risk=risk())
        for index in range(3000):
            state.on_candle(candle(index))
        payload = state.series_json("RELIANCE", Timeframe.M15)
        assert payload is not None
        assert len(payload["closed"]) <= 1500

    def test_partial_and_estimated_flags_survive_to_the_browser(self) -> None:
        state = DashboardState(risk=risk())
        state.on_candle(candle(0, partial=True))
        payload = state.series_json("RELIANCE", Timeframe.M15)
        assert payload is not None
        assert payload["closed"][0]["partial"] is True

    def test_concurrent_writes_and_reads_do_not_tear(self) -> None:
        state = DashboardState(risk=risk())
        errors: list[BaseException] = []

        def write() -> None:
            try:
                for index in range(500):
                    state.on_candle(candle(index))
            except BaseException as error:
                errors.append(error)

        def read() -> None:
            try:
                for _ in range(500):
                    state.series_json("RELIANCE", Timeframe.M15)
                    state.health_json()
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=write), threading.Thread(target=read)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []


class TestRiskPanel:
    def test_it_says_whether_the_bar_cleared_costs(self) -> None:
        state = DashboardState(risk=risk())
        state.on_candle(candle(0, price=100.0))  # 2-rupee range on 100 = 2%
        payload = state.risk_json("RELIANCE", Timeframe.M15)
        assert payload["clears_costs"] is True

        quiet = DashboardState(risk=risk())
        quiet.on_candle(
            Candle(
                instrument_key="X", timeframe=Timeframe.M15,
                ts=dt.datetime(2026, 8, 7, 9, 15, tzinfo=IST),
                open=100.0, high=100.01, low=100.0, close=100.0, volume=10,
            )
        )
        assert quiet.risk_json("X", Timeframe.M15)["clears_costs"] is False

    def test_a_tight_stop_is_flagged_as_leverage(self) -> None:
        """The error the project's own worked example produced: a 2x 15m ATR
        stop on 1% risk came out at 165% of capital."""
        tight = RiskContext(
            capital_inr=500_000.0, risk_fraction=0.01,
            breakeven_pct=0.00083, atr_pct=0.003,
        )
        state = DashboardState(risk=tight)
        state.on_candle(candle(0, price=1330.0))
        sizing = state.risk_json("RELIANCE", Timeframe.M15)["sizing"]
        assert sizing["exposure_pct"] > 1.0   # levered, and the UI says so

    def test_no_bars_yet_reports_the_hurdle_without_inventing_a_price(self) -> None:
        state = DashboardState(risk=risk())
        payload = state.risk_json("NOTHING", Timeframe.M15)
        assert payload["breakeven_pct"] == pytest.approx(0.00083)
        assert "price" not in payload


class TestServer:
    def test_it_refuses_to_bind_beyond_localhost(self) -> None:
        """No authentication, and the payload includes capital and sizing."""
        state = DashboardState(risk=risk())
        for host in ("0.0.0.0", "192.168.1.5"):
            with pytest.raises(DashboardBindError, match="refusing to bind"):
                build_server(state, host=host, port=0)

    def test_localhost_is_allowed(self) -> None:
        state = DashboardState(risk=risk())
        server = build_server(state, host="127.0.0.1", port=0)
        server.server_close()

    def test_the_routes_answer(self) -> None:
        state = DashboardState(risk=risk())
        state.on_candle(candle(0))
        state.set_health(StreamHealth(state=EngineState.LIVE, ticks_received=42))
        state.set_regime(dispersion=0.0021, correlation=0.196)

        server = build_server(state, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def fetch(path: str) -> Any:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as response:
                    return json.loads(response.read())

            health = fetch("/api/health")
            assert health["state"] == "live"
            assert health["ticks_received"] == 42
            assert health["regime"]["correlation"] == pytest.approx(0.196)

            symbols = fetch("/api/symbols")
            assert symbols["symbols"][0]["symbol"] == "RELIANCE"

            series = fetch("/api/candles?symbol=RELIANCE&timeframe=15m")
            assert len(series["closed"]) == 1

            assert "breakeven_pct" in fetch("/api/risk?symbol=RELIANCE&timeframe=15m")
        finally:
            server.shutdown()
            server.server_close()

    def test_a_bad_query_is_a_client_error_not_a_crash(self) -> None:
        state = DashboardState(risk=risk())
        server = build_server(state, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ("/api/candles", "/api/candles?symbol=X&timeframe=99y"):
                with pytest.raises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")
                assert caught.value.code == 400
        finally:
            server.shutdown()
            server.server_close()

    def test_there_is_no_route_that_mutates(self) -> None:
        """Alert-only, enforced at the transport layer as well as the adapter."""
        from nifty50.dashboard.server import _Handler

        assert hasattr(_Handler, "do_GET")
        for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
            assert not hasattr(_Handler, verb)
