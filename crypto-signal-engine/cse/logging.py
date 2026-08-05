"""Structured logging: JSON to a rotating file, human-readable to the console.

Every disconnect, rate-limit stall, integrity repair, and reconciliation
mismatch goes through here. The file output is JSON so that a run can be
audited afterwards with ``duckdb`` or ``jq`` — which matters when you are
trying to work out whether a signal fired on clean data.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog

from cse.config import LoggingConfig

_configured = False


def configure_logging(config: LoggingConfig, *, force: bool = False) -> None:
    """Install the structlog + stdlib logging pipeline. Idempotent."""
    global _configured
    if _configured and not force:
        return

    config.directory.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.level)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        filename=config.directory / config.filename,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if config.json_to_file
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, file_renderer],
        )
    )
    root.addHandler(file_handler)

    if config.console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=shared_processors,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
                ],
            )
        )
        root.addHandler(console_handler)

    _configured = True


def get_logger(name: str, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. Safe to call before ``configure_logging``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_values:
        logger = logger.bind(**initial_values)
    return logger
