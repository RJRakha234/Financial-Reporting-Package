"""Structured logging: JSON lines to a rotating file, human-readable to console."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

from nifty50.config import Config
from nifty50.domain import IST

_configured = False


def _ist_timestamper(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Stamp every record in IST. Log lines and market data must agree on 'when'."""
    import datetime as dt

    event_dict["ts"] = dt.datetime.now(tz=IST).isoformat()
    return event_dict


def configure_logging(config: Config, *, force: bool = False) -> None:
    """Install the structlog + stdlib logging pipeline. Idempotent."""
    global _configured
    if _configured and not force:
        return

    log_dir = config.path(config.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path: Path = log_dir / config.logging.file

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=config.logging.max_bytes,
        backupCount=config.logging.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer()
            if config.logging.json_lines
            else structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
    )

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
            foreign_pre_chain=_SHARED_PROCESSORS,
        )
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(config.logging.level.upper())

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    _configured = True


_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    _ist_timestamper,
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Module-level logger. Safe to call before :func:`configure_logging`."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
