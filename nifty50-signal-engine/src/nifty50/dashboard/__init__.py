"""Local, read-only dashboard: live charts, cost hurdle, sizing, engine health.

Binds to localhost only and handles no verb but GET. It cannot place an order
and has no route that mutates anything.
"""

from nifty50.dashboard.server import build_server, serve_in_background
from nifty50.dashboard.state import DashboardState, RiskContext

__all__ = ["DashboardState", "RiskContext", "build_server", "serve_in_background"]
