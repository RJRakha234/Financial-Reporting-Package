"""Load and validate the YAML configuration.

The config defines the SMTP server used to send, an optional IMAP server used
to detect replies, sensible job defaults, and a list of scheduled jobs.

Secrets are **never** stored in the file: passwords are read at run time from
the environment variable named by ``password_env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


# ---------------------------------------------------------------------------
# Server settings
# ---------------------------------------------------------------------------

_VALID_SECURITY = {"starttls", "ssl", "none"}


@dataclass
class ServerConfig:
    host: str
    port: int
    username: str
    password_env: str
    security: str = "starttls"
    timeout: int = 30

    def password(self) -> str:
        """Resolve the password from the environment at call time."""
        value = os.environ.get(self.password_env)
        if not value:
            raise ConfigError(
                f"environment variable {self.password_env!r} is not set; "
                "it must contain the password for "
                f"{self.username} on {self.host}"
            )
        return value


@dataclass
class SmtpConfig(ServerConfig):
    from_addr: str | None = None

    def sender_address(self) -> str:
        return self.from_addr or self.username


@dataclass
class ImapConfig(ServerConfig):
    mailbox: str = "INBOX"


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

_VALID_EVERY = {"interval", "daily", "weekly", "once"}
_WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


@dataclass
class Schedule:
    every: str  # interval | daily | weekly | once
    seconds: int | None = None  # interval only (resolved from s/m/h)
    at: str | None = None  # "HH:MM" for daily/weekly; ISO datetime for once
    weekday: int | None = None  # 0=Mon .. 6=Sun, weekly only

    def describe(self) -> str:
        if self.every == "interval":
            return f"every {self.seconds}s"
        if self.every == "daily":
            return f"daily at {self.at}"
        if self.every == "weekly":
            name = [k for k, v in _WEEKDAYS.items() if v == self.weekday][0]
            return f"weekly on {name} at {self.at}"
        return f"once at {self.at or 'startup'}"


# ---------------------------------------------------------------------------
# Job + top-level config
# ---------------------------------------------------------------------------


@dataclass
class Job:
    name: str
    to: list[str]
    subject: str
    body: str = ""
    html_body: str | None = None
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    variables: dict[str, str] = field(default_factory=dict)
    schedule: Schedule | None = None
    save_dir: str | None = None
    track_replies: bool = True
    reply_window_days: int = 14

    def all_recipients(self) -> list[str]:
        return [*self.to, *self.cc, *self.bcc]


@dataclass
class Config:
    smtp: SmtpConfig
    imap: ImapConfig | None
    jobs: list[Job]
    database: str = "mailflow.db"
    save_dir: str = "sent_mail"
    poll_seconds: int = 60

    def job(self, name: str) -> Job:
        for job in self.jobs:
            if job.name == name:
                return job
        raise ConfigError(f"no job named {name!r} in configuration")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _require(mapping: dict, key: str, where: str) -> Any:
    if key not in mapping or mapping[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field {key!r}")
    return mapping[key]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ConfigError(f"expected a string or list, got {type(value).__name__}")


def _parse_server(raw: dict, where: str) -> dict:
    security = str(raw.get("security", "starttls")).lower()
    if security not in _VALID_SECURITY:
        raise ConfigError(
            f"{where}: security must be one of {sorted(_VALID_SECURITY)}, "
            f"got {security!r}"
        )
    return {
        "host": _require(raw, "host", where),
        "port": int(_require(raw, "port", where)),
        "username": _require(raw, "username", where),
        "password_env": _require(raw, "password_env", where),
        "security": security,
        "timeout": int(raw.get("timeout", 30)),
    }


def _parse_schedule(raw: dict | None, where: str) -> Schedule | None:
    if raw is None:
        return None
    every = str(_require(raw, "every", where)).lower()
    if every not in _VALID_EVERY:
        raise ConfigError(
            f"{where}: schedule.every must be one of {sorted(_VALID_EVERY)}, "
            f"got {every!r}"
        )

    if every == "interval":
        seconds = raw.get("seconds")
        if seconds is None:
            minutes = raw.get("minutes")
            hours = raw.get("hours")
            seconds = (
                (int(minutes) * 60 if minutes is not None else 0)
                + (int(hours) * 3600 if hours is not None else 0)
            )
        seconds = int(seconds)
        if seconds <= 0:
            raise ConfigError(
                f"{where}: interval schedule needs a positive "
                "seconds/minutes/hours value"
            )
        return Schedule(every="interval", seconds=seconds)

    if every == "daily":
        return Schedule(every="daily", at=_parse_hhmm(raw, where))

    if every == "weekly":
        weekday_raw = str(_require(raw, "weekday", where)).lower()[:3]
        if weekday_raw not in _WEEKDAYS:
            raise ConfigError(
                f"{where}: weekday must be one of {sorted(_WEEKDAYS)}, "
                f"got {weekday_raw!r}"
            )
        return Schedule(
            every="weekly",
            weekday=_WEEKDAYS[weekday_raw],
            at=_parse_hhmm(raw, where),
        )

    # once
    return Schedule(every="once", at=raw.get("at"))


def _parse_hhmm(raw: dict, where: str) -> str:
    at = str(_require(raw, "at", where))
    try:
        hh, mm = at.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
    except ValueError:
        raise ConfigError(
            f"{where}: 'at' must be a 24h time HH:MM, got {at!r}"
        ) from None
    return f"{int(hh):02d}:{int(mm):02d}"


def _parse_job(raw: dict, defaults: dict) -> Job:
    name = _require(raw, "name", "job")
    where = f"job {name!r}"
    return Job(
        name=str(name),
        to=_as_list(_require(raw, "to", where)),
        subject=str(_require(raw, "subject", where)),
        body=str(raw.get("body", "")),
        html_body=raw.get("html_body"),
        cc=_as_list(raw.get("cc")),
        bcc=_as_list(raw.get("bcc")),
        attachments=_as_list(raw.get("attachments")),
        variables={str(k): str(v) for k, v in (raw.get("variables") or {}).items()},
        schedule=_parse_schedule(raw.get("schedule"), where),
        save_dir=raw.get("save_dir", defaults.get("save_dir")),
        track_replies=bool(raw.get("track_replies", defaults.get("track_replies", True))),
        reply_window_days=int(
            raw.get("reply_window_days", defaults.get("reply_window_days", 14))
        ),
    )


def load_config(path: str | Path) -> Config:
    """Parse and validate a YAML config file into a :class:`Config`."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("top level of config must be a mapping")

    smtp_raw = _require(raw, "smtp", "config")
    smtp = SmtpConfig(
        **_parse_server(smtp_raw, "smtp"),
        from_addr=smtp_raw.get("from_addr"),
    )

    imap = None
    if raw.get("imap"):
        imap_raw = raw["imap"]
        imap = ImapConfig(
            **_parse_server(imap_raw, "imap"),
            mailbox=imap_raw.get("mailbox", "INBOX"),
        )

    defaults = raw.get("defaults") or {}
    jobs_raw = raw.get("jobs") or []
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ConfigError("config must define at least one job under 'jobs'")

    jobs = [_parse_job(j, defaults) for j in jobs_raw]
    names = [j.name for j in jobs]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"duplicate job name(s): {sorted(dupes)}")

    return Config(
        smtp=smtp,
        imap=imap,
        jobs=jobs,
        database=str(raw.get("database", "mailflow.db")),
        save_dir=str(defaults.get("save_dir", "sent_mail")),
        poll_seconds=int(raw.get("poll_seconds", 60)),
    )
