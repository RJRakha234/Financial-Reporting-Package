"""Load configuration from a YAML file and/or environment variables.

Secrets (API keys, Telegram credentials) should come from the environment, not
the YAML file. The YAML holds non-secret tuning: which broker, risk limits, and
which Telegram chats to watch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .risk import RiskConfig


@dataclass
class TelegramConfig:
    api_id: int | None = None
    api_hash: str | None = None
    # Channel/group usernames or numeric ids to listen to. Empty = all.
    chats: tuple[str, ...] = ()
    session: str = "signaltrader"


@dataclass
class AppConfig:
    broker: str = "paper"
    dry_run: bool = True
    risk: RiskConfig = field(default_factory=RiskConfig)
    broker_config: dict = field(default_factory=dict)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)


def _env(name: str) -> str | None:
    val = os.environ.get(name)
    return val if val else None


def load_config(path: str | None = None) -> AppConfig:
    """Build an :class:`AppConfig`, layering env vars over an optional YAML file."""
    data: dict = {}
    if path:
        import yaml  # optional dependency, only needed when a file is given

        with open(path) as fh:
            data = yaml.safe_load(fh) or {}

    risk_data = data.get("risk", {}) or {}
    risk = RiskConfig(
        risk_per_trade=risk_data.get("risk_per_trade", 0.02),
        max_notional=risk_data.get("max_notional", 1_000.0),
        max_leverage=risk_data.get("max_leverage", 10),
        symbol_allowlist=tuple(risk_data.get("symbol_allowlist", ()) or ()),
        require_stop_loss=risk_data.get("require_stop_loss", False),
    )

    broker = data.get("broker", "paper")
    # dry_run defaults to True everywhere; live trading must be opted into.
    dry_run = bool(data.get("dry_run", True))
    if _env("SIGNALTRADER_LIVE") == "1":
        dry_run = False

    broker_config = dict(data.get("broker_config", {}) or {})
    # Pull secrets from the environment so they never live in the YAML.
    for key, env_name in {
        "api_key": "EXCHANGE_API_KEY",
        "api_secret": "EXCHANGE_API_SECRET",
        "access_token": "EXCHANGE_ACCESS_TOKEN",
    }.items():
        if _env(env_name):
            broker_config.setdefault(key, _env(env_name))

    tg_data = data.get("telegram", {}) or {}
    api_id = tg_data.get("api_id") or _env("TELEGRAM_API_ID")
    telegram = TelegramConfig(
        api_id=int(api_id) if api_id else None,
        api_hash=tg_data.get("api_hash") or _env("TELEGRAM_API_HASH"),
        chats=tuple(str(c) for c in (tg_data.get("chats", ()) or ())),
        session=tg_data.get("session", "signaltrader"),
    )

    return AppConfig(
        broker=broker,
        dry_run=dry_run,
        risk=risk,
        broker_config=broker_config,
        telegram=telegram,
    )
