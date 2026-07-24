"""reportbot — watch an Outlook mailbox and auto-download reports from a portal.

When an email matching one of your trigger rules lands in the mailbox, the bot
logs into a configured portal, downloads the report(s), and saves them to disk.

Public API::

    from reportbot import Config, Runner, build_mail_client

    config = Config.load("config.yaml")
    runner = Runner(config)
    runner.run_once()      # check now, download anything new
    runner.run_forever()   # poll on a loop

The mail backend (Microsoft Graph or IMAP) and the portal downloader are
selected from config, so the same runner works for either.
"""

from .config import Config, MailConfig, PortalConfig
from .mail import build_mail_client
from .mail.base import MailClient, Message
from .runner import Runner
from .state import StateStore
from .triggers import TriggerRule, matching_rule

__all__ = [
    "Config",
    "MailConfig",
    "PortalConfig",
    "Message",
    "MailClient",
    "build_mail_client",
    "Runner",
    "StateStore",
    "TriggerRule",
    "matching_rule",
]
