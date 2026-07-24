"""Load and validate the bot configuration.

Config lives in a YAML file. Any ``${VAR}`` in a string value is expanded from
the environment, so secrets (passwords, client secrets) stay in ``.env`` /
environment variables and never in the committed YAML.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .triggers import TriggerRule

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` references using ``os.environ``.

    A reference to an unset variable expands to an empty string; that keeps the
    loader from crashing when a placeholder has not been filled in yet — the
    backend that needs the value raises a clear error at use time instead.
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


@dataclass
class MailConfig:
    """How to connect to the mailbox and which folder to watch."""

    backend: str = "graph"  # "graph" | "imap"
    folder: str = "Inbox"
    # Look back this many days on the first run so old mail isn't downloaded.
    lookback_days: int = 1

    # Microsoft Graph (client-credentials flow)
    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""
    user: str = ""  # the mailbox UPN/email to read, e.g. me@contoso.com

    # IMAP
    host: str = "outlook.office365.com"
    port: int = 993
    username: str = ""
    password: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailConfig":
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        return cls(**known)


@dataclass
class PortalConfig:
    """How to log into the portal and grab the report file(s)."""

    login_url: str = ""
    reports_url: str = ""
    username: str = ""
    password: str = ""
    headless: bool = True
    timeout_ms: int = 30_000
    # CSS selectors for the login form and the download control.
    selectors: dict[str, str] = field(
        default_factory=lambda: {
            "username": "#username",
            "password": "#password",
            "submit": "button[type=submit]",
            "download": "a.download-report",
        }
    )
    # Optional: pull a direct report URL out of the triggering email body with
    # this regex (first capture group). When set, the bot fetches that link
    # instead of navigating the portal UI.
    link_regex: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PortalConfig":
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        merged = cls(**{k: v for k, v in known.items() if k != "selectors"})
        if "selectors" in data and data["selectors"]:
            merged.selectors = {**merged.selectors, **data["selectors"]}
        return merged


@dataclass
class Config:
    poll_interval_seconds: int = 60
    download_dir: str = "./downloads"
    state_file: str = "./reportbot_state.json"
    mail: MailConfig = field(default_factory=MailConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    triggers: list[TriggerRule] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        data = _expand_env(dict(data or {}))
        return cls(
            poll_interval_seconds=int(data.get("poll_interval_seconds", 60)),
            download_dir=str(data.get("download_dir", "./downloads")),
            state_file=str(data.get("state_file", "./reportbot_state.json")),
            mail=MailConfig.from_dict(data.get("mail", {}) or {}),
            portal=PortalConfig.from_dict(data.get("portal", {}) or {}),
            triggers=[
                TriggerRule.from_dict(t) for t in (data.get("triggers") or [])
            ],
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        try:
            import yaml
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "PyYAML is required to load config files. "
                "Install it with: pip install pyyaml"
            ) from exc
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text) or {})
