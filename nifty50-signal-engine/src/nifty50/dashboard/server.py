"""A local, read-only HTTP server for the live dashboard.

Deliberately built on :mod:`http.server` rather than a web framework. This
serves one operator on one machine at a cadence set by 15-minute bars; adding
FastAPI or Flask would bring a dependency tree, an ASGI server and a
configuration surface to solve a problem that does not exist here. The browser
polls, which at this cadence is indistinguishable from a websocket and has no
reconnect logic to get wrong.

**It binds to 127.0.0.1 and refuses to do otherwise.** There is no
authentication, and the payload includes your capital and position sizing. On a
shared network an unauthenticated bind to 0.0.0.0 publishes that to anyone who
scans the port. If you genuinely need remote access, put it behind an SSH
tunnel — do not change the bind.

Read-only by construction: the only verb handled is GET, and there is no route
that mutates anything. That matches the project's hard constraint — this tool
alerts, it never places an order — and it means a stray request cannot do
damage even in principle.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from nifty50.dashboard.state import DashboardState
from nifty50.domain import Timeframe
from nifty50.logging_setup import get_logger

log = get_logger(__name__)

_STATIC = Path(__file__).parent / "static"

# Binding anywhere else exposes capital and sizing to the local network with no
# authentication in front of it.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DashboardBindError(ValueError):
    """Raised when asked to bind somewhere that would expose the dashboard."""


class _Handler(BaseHTTPRequestHandler):
    """Routes. One verb, no mutation."""

    state: DashboardState  # injected by :func:`build_server`

    server_version = "nifty50-dashboard"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if route == "/":
                self._send_file("index.html", "text/html; charset=utf-8")
            elif route == "/api/health":
                self._send_json(self.state.health_json())
            elif route == "/api/symbols":
                self._send_json(
                    {
                        "symbols": [
                            {"symbol": s, "timeframes": self.state.timeframes(s)}
                            for s in self.state.symbols()
                        ]
                    }
                )
            elif route == "/api/candles":
                self._send_json(self._candles(query))
            elif route == "/api/risk":
                symbol, timeframe = self._require_series(query)
                self._send_json(self.state.risk_json(symbol, timeframe))
            else:
                self._send_json({"error": f"no route {route}"}, HTTPStatus.NOT_FOUND)
        except _BadRequestError as error:
            self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:  # a dashboard must not take the stream down
            log.exception("dashboard_request_failed", route=route)
            self._send_json({"error": repr(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    # ------------------------------------------------------------- helpers

    def _candles(self, query: dict[str, list[str]]) -> dict[str, Any]:
        symbol, timeframe = self._require_series(query)
        limit_values = query.get("limit")
        limit = int(limit_values[0]) if limit_values else 400
        payload = self.state.series_json(symbol, timeframe, limit=limit)
        if payload is None:
            raise _BadRequestError(f"no series for {symbol} {timeframe.value}")
        return payload

    def _require_series(self, query: dict[str, list[str]]) -> tuple[str, Timeframe]:
        symbols = query.get("symbol")
        timeframes = query.get("timeframe")
        if not symbols or not timeframes:
            raise _BadRequestError("symbol and timeframe are both required")
        try:
            return symbols[0], Timeframe(timeframes[0])
        except ValueError as error:
            raise _BadRequestError(f"unknown timeframe {timeframes[0]!r}") from error

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str, content_type: str) -> None:
        path = _STATIC / name
        if not path.is_file():
            self._send_json({"error": f"missing {name}"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Every poll would otherwise print a line to stderr, once a second,
        # for the whole session -- burying anything that matters.
        log.debug("dashboard_request", request=format % args)


class _BadRequestError(ValueError):
    """A malformed query, distinguished from a server fault."""


def build_server(state: DashboardState, *, host: str, port: int) -> ThreadingHTTPServer:
    """Construct the server, refusing any bind that would expose it."""
    if host not in _ALLOWED_HOSTS:
        raise DashboardBindError(
            f"refusing to bind to {host!r}. The dashboard has no authentication "
            f"and its payload includes your capital and position sizing. "
            f"Use one of {sorted(_ALLOWED_HOSTS)}, and an SSH tunnel for remote access."
        )
    handler = type("_BoundHandler", (_Handler,), {"state": state})
    return ThreadingHTTPServer((host, port), handler)


def serve_in_background(
    state: DashboardState, *, host: str, port: int
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the server on a daemon thread and return both for shutdown."""
    server = build_server(state, host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard_started", url=f"http://{host}:{port}")
    return server, thread
