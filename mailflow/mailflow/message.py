"""Build MIME e-mail messages from jobs, and save them to disk as ``.eml``.

Subjects and bodies support ``{placeholder}`` substitution. Built-in
placeholders are ``{date}``, ``{time}``, ``{datetime}`` and ``{job}``; any
extra keys come from a job's ``variables`` map. Unknown placeholders are left
untouched so literal braces in a body survive.
"""

from __future__ import annotations

import glob
import mimetypes
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path

from .config import Job, SmtpConfig


class _SafeDict(dict):
    """Format mapping that leaves unknown ``{keys}`` in place."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(text: str, context: dict[str, str]) -> str:
    """Substitute ``{placeholders}`` without choking on stray braces."""
    try:
        return text.format_map(_SafeDict(context))
    except (ValueError, IndexError):
        # A lone/positional brace in the template — return it verbatim.
        return text


def build_context(job: Job, now: datetime | None = None) -> dict[str, str]:
    now = now or datetime.now()
    context = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "job": job.name,
    }
    context.update(job.variables)
    return context


def resolve_attachments(patterns: list[str]) -> list[Path]:
    """Expand glob patterns into concrete, existing file paths."""
    paths: list[Path] = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if not matches:
            raise FileNotFoundError(
                f"attachment pattern matched no files: {pattern}"
            )
        for match in matches:
            path = Path(match)
            if path.is_file():
                paths.append(path)
    return paths


def build_message(job: Job, smtp: SmtpConfig, now: datetime | None = None) -> EmailMessage:
    """Compose an :class:`EmailMessage` for ``job`` ready to send or save."""
    context = build_context(job, now)
    msg = EmailMessage()
    msg["From"] = smtp.sender_address()
    msg["To"] = ", ".join(job.to)
    if job.cc:
        msg["Cc"] = ", ".join(job.cc)
    msg["Subject"] = render(job.subject, context)
    msg["Date"] = formatdate(localtime=True)

    domain = parseaddr(smtp.sender_address())[1].split("@")[-1] or None
    msg["Message-ID"] = make_msgid(domain=domain)

    body = render(job.body, context)
    msg.set_content(body or "")
    if job.html_body:
        msg.add_alternative(render(job.html_body, context), subtype="html")

    for path in resolve_attachments(job.attachments):
        ctype, encoding = mimetypes.guess_type(path.name)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )
    return msg


def _safe_filename(text: str) -> str:
    keep = "-_. "
    cleaned = "".join(c if c.isalnum() or c in keep else "_" for c in text)
    return cleaned.strip().replace(" ", "_")[:80] or "message"


def save_message(msg: EmailMessage, save_dir: str | Path, job_name: str) -> Path:
    """Write ``msg`` to ``save_dir`` as a timestamped ``.eml`` file."""
    return save_raw(bytes(msg), save_dir, job_name)


def save_raw(raw: bytes, save_dir: str | Path, name: str) -> Path:
    """Write raw RFC822 ``bytes`` to ``save_dir`` as a timestamped ``.eml``."""
    directory = Path(save_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stamp}_{_safe_filename(name)}.eml"
    path.write_bytes(raw)
    return path
