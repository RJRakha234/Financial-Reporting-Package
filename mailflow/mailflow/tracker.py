"""SQLite-backed record of every send and its revert (reply) status.

Two tables:

* ``sends`` — one row per delivered (or failed) message, including its
  ``Message-ID``, the path it was saved to, and whether a reply has been
  recorded (``status`` is ``sent`` / ``replied`` / ``failed``).
* ``job_runs`` — the last time each job fired, used to compute the next run.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT    NOT NULL,
    message_id  TEXT,
    subject     TEXT,
    to_addrs    TEXT,
    cc_addrs    TEXT,
    bcc_addrs   TEXT,
    sent_at     TEXT    NOT NULL,
    saved_path  TEXT,
    status      TEXT    NOT NULL DEFAULT 'sent',
    error       TEXT,
    replied_at  TEXT,
    reply_from  TEXT,
    last_checked TEXT
);
CREATE TABLE IF NOT EXISTS job_runs (
    job       TEXT PRIMARY KEY,
    last_run  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_status ON sends(status);
CREATE INDEX IF NOT EXISTS idx_sends_message_id ON sends(message_id);
"""


@dataclass
class SendRecord:
    id: int
    job: str
    message_id: str | None
    subject: str | None
    to_addrs: str | None
    sent_at: str
    saved_path: str | None
    status: str
    error: str | None
    replied_at: str | None
    reply_from: str | None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Tracker:
    """Thin wrapper over a SQLite database. Use as a context manager."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Tracker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- recording sends ---------------------------------------------------
    def record_send(
        self,
        job: str,
        message_id: str | None,
        subject: str | None,
        to_addrs: list[str],
        cc_addrs: list[str],
        bcc_addrs: list[str],
        saved_path: str | None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO sends
                (job, message_id, subject, to_addrs, cc_addrs, bcc_addrs,
                 sent_at, saved_path, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sent')
            """,
            (
                job,
                message_id,
                subject,
                ", ".join(to_addrs),
                ", ".join(cc_addrs),
                ", ".join(bcc_addrs),
                _now(),
                saved_path,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_failure(
        self, job: str, subject: str | None, to_addrs: list[str], error: str
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO sends
                (job, subject, to_addrs, sent_at, status, error)
            VALUES (?, ?, ?, ?, 'failed', ?)
            """,
            (job, subject, ", ".join(to_addrs), _now(), error),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # -- revert / reply status --------------------------------------------
    def mark_replied(
        self, send_id: int, reply_from: str | None = None, when: str | None = None
    ) -> bool:
        cur = self.conn.execute(
            """
            UPDATE sends
               SET status = 'replied', replied_at = ?, reply_from = ?,
                   last_checked = ?
             WHERE id = ? AND status != 'replied'
            """,
            (when or _now(), reply_from, _now(), send_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def touch_checked(self, send_id: int) -> None:
        self.conn.execute(
            "UPDATE sends SET last_checked = ? WHERE id = ?", (_now(), send_id)
        )
        self.conn.commit()

    def pending_reply_sends(self, window_days: int | None = None) -> list[SendRecord]:
        """Sends that were delivered but have no reply recorded yet."""
        rows = self.conn.execute(
            "SELECT * FROM sends WHERE status = 'sent' AND message_id IS NOT NULL "
            "ORDER BY sent_at"
        ).fetchall()
        records = [self._to_record(r) for r in rows]
        if window_days is None:
            return records
        cutoff = datetime.now() - timedelta(days=window_days)
        return [
            r for r in records if datetime.fromisoformat(r.sent_at) >= cutoff
        ]

    # -- queries -----------------------------------------------------------
    def all_sends(self, job: str | None = None, limit: int | None = None) -> list[SendRecord]:
        sql = "SELECT * FROM sends"
        params: list[object] = []
        if job:
            sql += " WHERE job = ?"
            params.append(job)
        sql += " ORDER BY id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._to_record(r) for r in self.conn.execute(sql, params)]

    def find_by_id(self, send_id: int) -> SendRecord | None:
        row = self.conn.execute(
            "SELECT * FROM sends WHERE id = ?", (send_id,)
        ).fetchone()
        return self._to_record(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM sends GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # -- job run bookkeeping ----------------------------------------------
    def last_run(self, job: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT last_run FROM job_runs WHERE job = ?", (job,)
        ).fetchone()
        return datetime.fromisoformat(row["last_run"]) if row else None

    def set_last_run(self, job: str, when: datetime | None = None) -> None:
        ts = (when or datetime.now()).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO job_runs (job, last_run) VALUES (?, ?) "
            "ON CONFLICT(job) DO UPDATE SET last_run = excluded.last_run",
            (job, ts),
        )
        self.conn.commit()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _to_record(row: sqlite3.Row) -> SendRecord:
        return SendRecord(
            id=row["id"],
            job=row["job"],
            message_id=row["message_id"],
            subject=row["subject"],
            to_addrs=row["to_addrs"],
            sent_at=row["sent_at"],
            saved_path=row["saved_path"],
            status=row["status"],
            error=row["error"],
            replied_at=row["replied_at"],
            reply_from=row["reply_from"],
        )
