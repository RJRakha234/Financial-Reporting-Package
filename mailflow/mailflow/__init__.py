"""mailflow — schedule outgoing e-mail, archive each message, and track reverts.

A standalone tool to send corporate mail on a schedule, save a copy of every
message to disk, and record whether each one was replied to ("reverted").

Library usage::

    from mailflow import load_config, Tracker, SmtpSender, send_job, run_due

    config = load_config("mailflow.yaml")
    with Tracker(config.database) as tracker:
        sender = SmtpSender(config.smtp)
        run_due(config, sender, tracker)        # fire all due jobs
        send_job(config.job("weekly"), config, sender, tracker)  # one job now
"""

from .config import (
    Config,
    ConfigError,
    ImapConfig,
    Job,
    Schedule,
    SmtpConfig,
    load_config,
)
from .excel import (
    ExcelError,
    Spreadsheet,
    check_replies,
    make_template,
    send_due,
)
from .message import build_message, save_message, save_raw
from .replies import ImapReplyChecker, ReplyCheckResult
from .scheduler import due_jobs, is_due, next_run, run_due, run_forever, send_job
from .sender import DryRunSender, Sender, SmtpSender
from .tracker import SendRecord, Tracker

__version__ = "0.1.0"

__all__ = [
    "Config",
    "ConfigError",
    "ImapConfig",
    "Job",
    "Schedule",
    "SmtpConfig",
    "load_config",
    "ExcelError",
    "Spreadsheet",
    "check_replies",
    "make_template",
    "send_due",
    "build_message",
    "save_message",
    "save_raw",
    "ImapReplyChecker",
    "ReplyCheckResult",
    "due_jobs",
    "is_due",
    "next_run",
    "run_due",
    "run_forever",
    "send_job",
    "DryRunSender",
    "Sender",
    "SmtpSender",
    "SendRecord",
    "Tracker",
]
