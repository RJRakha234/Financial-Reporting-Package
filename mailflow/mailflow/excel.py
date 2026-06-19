"""Spreadsheet-driven mail: each row of an ``.xlsx`` is one e-mail.

The spreadsheet is both the **input** (what to send, to whom, and when) and the
live **status board** (the tool writes Sent/Received status, the reply sender,
and the archive paths back into the same rows).

Recognised input columns (header names are case-insensitive; several aliases
are accepted):

    To            recipient address(es), separated by , or ;
    CC            optional cc address(es)
    Subject       the subject line (supports {Column} placeholders)
    Body          the message body  (supports {Column} placeholders)
    Attachments   file path(s)/glob(s), separated by ; or new lines
    Send Time     when to send (blank = as soon as the tool runs)

Any *other* column can be referenced as a ``{Column Name}`` placeholder in the
Subject or Body — i.e. mail-merge straight from the sheet.

Columns the tool fills in (created automatically if absent):

    Sent Status · Sent At · Message ID · Sent Path
    Received Status · Received From · Received At · Received Path · Notes

Server credentials never live in the spreadsheet — they come from the YAML
config / environment, exactly as for the rest of mailflow.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .config import Config, Job
from .message import build_message, save_message, save_raw
from .replies import ImapReplyChecker
from .sender import Sender

log = logging.getLogger("mailflow")

# -- column resolution -------------------------------------------------------

INPUT_ALIASES: dict[str, list[str]] = {
    "to": ["to", "recipient", "recipients", "to recipients", "send to", "mail to"],
    "cc": ["cc", "cc recipients", "carbon copy"],
    "subject": ["subject", "mail subject", "title"],
    "body": ["body", "message", "mail body", "content", "mail content", "mail"],
    "attachments": ["attachments", "attachment", "files", "file", "attachment path"],
    "send_time": ["send time", "timer", "schedule", "send at", "scheduled time",
                  "send date", "time", "when"],
    "save_dir": ["save dir", "save folder", "sent folder", "sent dir"],
}

OUTPUT_HEADERS: dict[str, str] = {
    "sent_status": "Sent Status",
    "sent_at": "Sent At",
    "message_id": "Message ID",
    "sent_path": "Sent Path",
    "received_status": "Received Status",
    "received_from": "Received From",
    "received_at": "Received At",
    "received_path": "Received Path",
    "error": "Notes",
}

_SENT = "Sent"
_FAILED = "Failed"
_AWAITING = "Awaiting"
_RECEIVED = "Received"


class ExcelError(Exception):
    """Raised for spreadsheet structure problems."""


@dataclass
class SendSummary:
    sent: int = 0
    failed: int = 0
    skipped_not_due: int = 0
    already_sent: int = 0


@dataclass
class ReplySummary:
    checked: int = 0
    received: int = 0


# -- spreadsheet wrapper -----------------------------------------------------


class Spreadsheet:
    """Thin openpyxl wrapper: header lookup, cell get/set, column creation."""

    def __init__(self, path: str | Path, sheet: str | None = None):
        self.path = Path(path)
        if not self.path.is_file():
            raise ExcelError(f"spreadsheet not found: {self.path}")
        self.wb = openpyxl.load_workbook(self.path)
        self.ws = self.wb[sheet] if sheet else self.wb.active
        self.headers: dict[str, int] = {}
        for col in range(1, self.ws.max_column + 1):
            val = self.ws.cell(row=1, column=col).value
            if val is not None and str(val).strip():
                self.headers[str(val).strip().lower()] = col

    def col(self, *names: str) -> int | None:
        for name in names:
            idx = self.headers.get(name.strip().lower())
            if idx:
                return idx
        return None

    def ensure_col(self, header_text: str) -> int:
        idx = self.col(header_text)
        if idx:
            return idx
        idx = self.ws.max_column + 1
        cell = self.ws.cell(row=1, column=idx, value=header_text)
        cell.font = Font(bold=True)
        self.headers[header_text.strip().lower()] = idx
        return idx

    def data_rows(self) -> list[int]:
        return list(range(2, self.ws.max_row + 1))

    def get(self, row: int, col: int | None) -> object:
        return self.ws.cell(row=row, column=col).value if col else None

    def set(self, row: int, col: int, value) -> None:
        self.ws.cell(row=row, column=col, value=value)

    def row_dict(self) -> dict[int, str]:
        """Map column index -> original header text (for placeholders)."""
        out = {}
        for col in range(1, self.ws.max_column + 1):
            val = self.ws.cell(row=1, column=col).value
            if val is not None and str(val).strip():
                out[col] = str(val).strip()
        return out

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path else self.path
        self.wb.save(target)
        return target


# -- value parsing -----------------------------------------------------------


def split_addresses(value: object) -> list[str]:
    if value is None:
        return []
    parts = re.split(r"[,;\n]+", str(value))
    return [p.strip() for p in parts if p.strip()]


def split_paths(value: object) -> list[str]:
    if value is None:
        return []
    parts = re.split(r"[;\n]+", str(value))
    return [p.strip() for p in parts if p.strip()]


_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    "%d-%m-%Y %H:%M", "%d-%m-%Y",
)


def parse_when(value: object) -> datetime | None:
    """Parse a Send Time cell. Blank means 'send now'."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if s.lower() in {"now", "asap", "immediately"}:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        raise ExcelError(
            f"unrecognised Send Time {value!r}; use e.g. '2026-06-22 08:00'"
        ) from None


def _context(sheet: Spreadsheet, row: int, now: datetime) -> dict[str, str]:
    """Build the {placeholder} context from every column of the row."""
    ctx: dict[str, str] = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
    }
    for col, header in sheet.row_dict().items():
        val = sheet.get(row, col)
        text = "" if val is None else str(val)
        # Expose several spellings so {First Name}/{First_Name}/{first_name} all work.
        for key in {header, header.replace(" ", "_"), header.lower(),
                    header.lower().replace(" ", "_")}:
            ctx[key] = text
    return ctx


# -- sending -----------------------------------------------------------------


def _require_columns(sheet: Spreadsheet) -> dict[str, int | None]:
    cols = {key: sheet.col(*aliases) for key, aliases in INPUT_ALIASES.items()}
    missing = [k for k in ("to", "subject") if cols[k] is None]
    if missing:
        found = ", ".join(sorted(sheet.headers)) or "(none)"
        raise ExcelError(
            f"spreadsheet is missing required column(s): {missing}. "
            f"Headers found: {found}"
        )
    return cols


def send_due(
    sheet: Spreadsheet,
    config: Config,
    sender: Sender,
    now: datetime | None = None,
    dry_run: bool = False,
) -> SendSummary:
    """Send every row whose Send Time has arrived and is not yet sent."""
    now = now or datetime.now()
    cols = _require_columns(sheet)
    out = {k: sheet.ensure_col(h) for k, h in OUTPUT_HEADERS.items()} if not dry_run else {}
    summary = SendSummary()

    for row in sheet.data_rows():
        to = split_addresses(sheet.get(row, cols["to"]))
        subject = sheet.get(row, cols["subject"])
        if not to or not subject:
            continue  # blank/spacer row

        status_col = out.get("sent_status") or sheet.col(OUTPUT_HEADERS["sent_status"])
        status = str(sheet.get(row, status_col) or "").strip().lower()
        if status == _SENT.lower():
            summary.already_sent += 1
            continue
        # Any other value (blank/Pending, or a previous 'Failed') is (re)tried.

        when = parse_when(sheet.get(row, cols["send_time"])) if cols["send_time"] else None
        if when is not None and when > now:
            summary.skipped_not_due += 1
            continue

        job = Job(
            name=f"row{row}",
            to=to,
            cc=split_addresses(sheet.get(row, cols["cc"])) if cols["cc"] else [],
            subject=str(subject),
            body=str(sheet.get(row, cols["body"]) or "") if cols["body"] else "",
            attachments=split_paths(sheet.get(row, cols["attachments"])) if cols["attachments"] else [],
            variables=_context(sheet, row, now),
        )

        if dry_run:
            log.info("[dry-run] row %s -> %s | %s", row, ", ".join(to), job.subject)
            summary.sent += 1
            continue

        try:
            msg = build_message(job, config.smtp, now)
            save_dir = (str(sheet.get(row, cols["save_dir"])) if cols["save_dir"] and sheet.get(row, cols["save_dir"]) else None) or config.save_dir
            saved = save_message(msg, save_dir, f"row{row}")
            sender.send(msg, job.all_recipients())
        except Exception as exc:  # noqa: BLE001
            sheet.set(row, out["sent_status"], _FAILED)
            sheet.set(row, out["error"], str(exc)[:300])
            summary.failed += 1
            log.error("row %s failed: %s", row, exc)
            continue

        sheet.set(row, out["sent_status"], _SENT)
        sheet.set(row, out["sent_at"], now.strftime("%Y-%m-%d %H:%M:%S"))
        sheet.set(row, out["message_id"], msg["Message-ID"])
        sheet.set(row, out["sent_path"], str(saved))
        sheet.set(row, out["received_status"], _AWAITING)
        sheet.set(row, out["error"], "")
        summary.sent += 1
        log.info("row %s sent to %s (saved %s)", row, ", ".join(to), saved)

    if not dry_run:
        sheet.save()
    return summary


# -- revert detection --------------------------------------------------------


def check_replies(
    sheet: Spreadsheet,
    config: Config,
    checker: ImapReplyChecker | None = None,
    now: datetime | None = None,
) -> ReplySummary:
    """Look for replies to sent rows; archive them and write status back."""
    if config.imap is None:
        raise ExcelError("no 'imap' section in config; cannot detect reverts")
    now = now or datetime.now()
    checker = checker or ImapReplyChecker(config.imap)

    out = {k: sheet.ensure_col(h) for k, h in OUTPUT_HEADERS.items()}
    summary = ReplySummary()

    conn = checker.connect()
    try:
        for row in sheet.data_rows():
            sent = str(sheet.get(row, out["sent_status"]) or "").strip().lower()
            recvd = str(sheet.get(row, out["received_status"]) or "").strip().lower()
            msg_id = str(sheet.get(row, out["message_id"]) or "").strip()
            if sent != _SENT.lower() or recvd == _RECEIVED.lower() or not msg_id:
                continue
            summary.checked += 1
            hit = checker.find_reply(conn, msg_id)
            if hit is None:
                continue
            saved = save_raw(hit.raw, config.received_dir, f"row{row}-revert")
            sheet.set(row, out["received_status"], _RECEIVED)
            sheet.set(row, out["received_from"], hit.from_addr)
            sheet.set(row, out["received_at"], hit.received_at or now.strftime("%Y-%m-%d %H:%M:%S"))
            sheet.set(row, out["received_path"], str(saved))
            summary.received += 1
            log.info("row %s revert from %s (saved %s)", row, hit.from_addr, saved)
    finally:
        checker._logout(conn)

    sheet.save()
    return summary


# -- template ----------------------------------------------------------------


def make_template(path: str | Path) -> Path:
    """Write a ready-to-edit .xlsx with headers and one example row."""
    path = Path(path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Mails"
    headers = [
        "To", "CC", "Subject", "Body", "Attachments", "Send Time",
        *OUTPUT_HEADERS.values(),
    ]
    ws.append(headers)
    for col in range(1, len(headers) + 1):
        ws.cell(row=1, column=col).font = Font(bold=True)
    ws.append([
        "cfo@example.com, controller@example.com",
        "audit@example.com",
        "Weekly Financial Report - {date}",
        "Hi team,\n\nPlease find this week's report attached. Kindly revert with approval.",
        "reports/weekly_latest.pdf",
        "2026-06-22 08:00",
    ])
    # Friendly column widths.
    widths = [34, 22, 32, 40, 26, 18, 12, 18, 30, 28, 14, 16, 18, 28, 24]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    wb.save(path)
    return path
