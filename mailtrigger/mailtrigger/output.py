"""Deliver the processed report: save to disk and/or email it back."""

from __future__ import annotations

import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .mailbox import Email
from .processor import Processed


def save_to_disk(result: Processed, output_cfg: dict) -> Path:
    save_dir = Path(output_cfg.get("save_dir", "./output"))
    save_dir.mkdir(parents=True, exist_ok=True)
    template = output_cfg.get("filename_template", "report_{timestamp}.{ext}")
    name = template.format(
        timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"),
        ext=result.ext,
    )
    path = save_dir / name
    path.write_bytes(result.content)
    return path


def email_result(result: Processed, saved: Path, output_cfg: dict, trigger_mail: Email) -> None:
    reply = output_cfg.get("email_reply") or {}
    if not reply.get("enabled"):
        return

    to_addr = (reply.get("to") or "").strip() or trigger_mail.from_addr
    msg = EmailMessage()
    msg["From"] = reply["username"]
    msg["To"] = to_addr
    msg["Subject"] = reply.get("subject", "Report result")
    msg.set_content(
        f"The report triggered by '{trigger_mail.subject}' has been generated.\n\n"
        f"Attached: {saved.name}\n"
    )
    msg.add_attachment(
        result.content,
        maintype="application",
        subtype="octet-stream",
        filename=saved.name,
    )

    host = reply["smtp_host"]
    port = int(reply.get("smtp_port", 587))
    with smtplib.SMTP(host, port) as smtp:
        smtp.starttls()
        smtp.login(reply["username"], reply["password"])
        smtp.send_message(msg)
