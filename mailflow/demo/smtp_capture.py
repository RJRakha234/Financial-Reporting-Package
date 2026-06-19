"""A tiny, dependency-free SMTP server that captures delivered mail to disk.

Used by the demo to show mailflow actually delivering over the network without
needing a real mail server. It speaks just enough SMTP for smtplib, writes each
received message to ``--out`` as a ``.eml``, and prints a one-line log per mail.

    python smtp_capture.py --host 127.0.0.1 --port 8025 --out inbox
"""

from __future__ import annotations

import argparse
import socketserver
from datetime import datetime
from email.parser import BytesParser
from pathlib import Path

OUT = Path("inbox")


class SMTPHandler(socketserver.StreamRequestHandler):
    def reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self) -> None:
        self.reply("220 demo.local ESMTP mailflow-capture")
        mail_from = ""
        rcpts: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            cmd = raw.decode(errors="replace").rstrip("\r\n")
            upper = cmd.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                # Advertise nothing fancy — no auth needed (internal relay).
                self.reply("250-demo.local greets you")
                self.reply("250 HELP")
            elif upper.startswith("MAIL FROM"):
                mail_from = cmd.split(":", 1)[1].strip()
                self.reply("250 2.1.0 OK")
            elif upper.startswith("RCPT TO"):
                rcpts.append(cmd.split(":", 1)[1].strip())
                self.reply("250 2.1.5 OK")
            elif upper == "DATA":
                self.reply("354 End data with <CR><LF>.<CR><LF>")
                self._read_data(mail_from, rcpts)
                self.reply("250 2.0.0 OK: queued")
                mail_from, rcpts = "", []
            elif upper == "QUIT":
                self.reply("221 2.0.0 Bye")
                break
            elif upper == "RSET":
                mail_from, rcpts = "", []
                self.reply("250 2.0.0 OK")
            elif upper == "NOOP":
                self.reply("250 2.0.0 OK")
            else:
                self.reply("250 2.0.0 OK")

    def _read_data(self, mail_from: str, rcpts: list[str]) -> None:
        lines: list[bytes] = []
        while True:
            raw = self.rfile.readline()
            if not raw or raw in (b".\r\n", b".\n"):
                break
            if raw.startswith(b".."):  # undo dot-stuffing
                raw = raw[1:]
            lines.append(raw)
        data = b"".join(lines)
        msg = BytesParser().parsebytes(data)
        OUT.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = OUT / f"received-{stamp}.eml"
        path.write_bytes(data)
        subject = msg.get("Subject", "(no subject)")
        print(
            f"[relay] delivered to {', '.join(rcpts) or '?'}  |  "
            f"subject: {subject}  ->  {path}",
            flush=True,
        )


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8025)
    ap.add_argument("--out", default="inbox")
    args = ap.parse_args()

    global OUT
    OUT = Path(args.out)
    print(f"[relay] capture SMTP server on {args.host}:{args.port}, "
          f"saving to {OUT}/", flush=True)
    with Server((args.host, args.port), SMTPHandler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
