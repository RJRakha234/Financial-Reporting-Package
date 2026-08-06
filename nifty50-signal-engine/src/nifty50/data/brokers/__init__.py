"""Broker adapters. Downstream code depends on :class:`BrokerAdapter`, never a SDK."""

from __future__ import annotations

from collections.abc import Callable

from nifty50.config import Config
from nifty50.data.brokers.base import (
    BrokerAdapter,
    ConnectionState,
    HistoricalRequest,
    InstrumentRegistry,
    ReadOnlyViolationError,
    StreamCallbacks,
    StreamMode,
    TokenStatus,
    assert_read_only,
)

__all__ = [
    "BrokerAdapter",
    "ConnectionState",
    "HistoricalRequest",
    "InstrumentRegistry",
    "ReadOnlyViolationError",
    "StreamCallbacks",
    "StreamMode",
    "TokenStatus",
    "assert_read_only",
    "build_adapter",
]


def _kite_factory(config: Config) -> BrokerAdapter:
    from nifty50.data.brokers.kite import KiteAdapter

    return KiteAdapter(config)


def _replay_factory(config: Config) -> BrokerAdapter:
    from nifty50.data.brokers.replay import ReplayAdapter

    return ReplayAdapter(config)


# Adding a vendor means adding a factory here and a section under `broker:` in
# config.yaml. No other module changes.
_FACTORIES: dict[str, Callable[[Config], BrokerAdapter]] = {
    "kite": _kite_factory,
    "replay": _replay_factory,
}


def build_adapter(config: Config, name: str | None = None) -> BrokerAdapter:
    """Instantiate the configured broker adapter."""
    key = name or config.broker.name
    try:
        factory = _FACTORIES[key]
    except KeyError:
        raise ValueError(
            f"unknown broker adapter {key!r}; available: {sorted(_FACTORIES)}"
        ) from None
    adapter = factory(config)
    assert_read_only(type(adapter))
    return adapter
