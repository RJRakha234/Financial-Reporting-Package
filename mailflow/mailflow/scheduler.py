"""Schedule arithmetic and the send loop.

``next_run`` computes when a job should next fire given when it last fired. A
job is *due* when that moment is at or before "now". For clock-based schedules
(daily/weekly) a single missed occurrence is caught up, so a ``run`` command
invoked periodically by cron will fire the job once even if it is run a little
late.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as dtime, timedelta

from .config import Config, Job, Schedule
from .message import build_message, save_message
from .sender import Sender
from .tracker import Tracker

log = logging.getLogger("mailflow")


def _parse_at(at: str) -> dtime:
    hh, mm = at.split(":")
    return dtime(int(hh), int(mm))


def next_run(schedule: Schedule, last_run: datetime | None, now: datetime) -> datetime | None:
    """The next datetime ``schedule`` should fire after ``last_run``.

    Returns ``None`` for a one-shot schedule that has already fired.
    """
    if schedule.every == "interval":
        anchor = last_run or (now - timedelta(seconds=schedule.seconds or 0))
        return anchor + timedelta(seconds=schedule.seconds or 0)

    if schedule.every == "daily":
        at = _parse_at(schedule.at or "00:00")
        if last_run is None:
            # First run: today's slot — due if it has already passed, else
            # it sits in the future and the job waits for it.
            return datetime.combine(now.date(), at)
        candidate = datetime.combine(last_run.date(), at)
        while candidate <= last_run:
            candidate += timedelta(days=1)
        return candidate

    if schedule.every == "weekly":
        at = _parse_at(schedule.at or "00:00")
        if last_run is None:
            # This week's slot on the target weekday (may be past or future).
            offset = schedule.weekday - now.weekday()
            return datetime.combine(now.date() + timedelta(days=offset), at)
        candidate = datetime.combine(last_run.date(), at)
        while candidate <= last_run or candidate.weekday() != schedule.weekday:
            candidate += timedelta(days=1)
        return candidate

    # once
    if last_run is not None:
        return None
    if schedule.at:
        try:
            return datetime.fromisoformat(schedule.at)
        except ValueError:
            return now
    return now


def is_due(job: Job, tracker: Tracker, now: datetime | None = None) -> bool:
    if job.schedule is None:
        return False
    now = now or datetime.now()
    nxt = next_run(job.schedule, tracker.last_run(job.name), now)
    return nxt is not None and nxt <= now


def due_jobs(config: Config, tracker: Tracker, now: datetime | None = None) -> list[Job]:
    now = now or datetime.now()
    return [j for j in config.jobs if is_due(j, tracker, now)]


def send_job(
    job: Job,
    config: Config,
    sender: Sender,
    tracker: Tracker,
    now: datetime | None = None,
    dry_run: bool = False,
) -> int | None:
    """Build, save, send and record one job.

    Returns the send-record id, or ``None`` for a dry run (which writes nothing
    to disk or the tracker and does not advance the schedule).
    """
    now = now or datetime.now()
    msg = build_message(job, config.smtp, now)
    subject = msg["Subject"]

    if dry_run:
        sender.send(msg, job.all_recipients())
        log.info("[dry-run] would send %s to %s", job.name, ", ".join(job.to))
        return None

    saved_path: str | None = None
    save_dir = job.save_dir or config.save_dir
    if save_dir:
        saved_path = str(save_message(msg, save_dir, job.name))

    try:
        sender.send(msg, job.all_recipients())
    except Exception as exc:  # noqa: BLE001 — record any delivery failure
        log.error("job %s failed to send: %s", job.name, exc)
        return tracker.record_failure(job.name, subject, job.to, str(exc))

    send_id = tracker.record_send(
        job=job.name,
        message_id=msg["Message-ID"],
        subject=subject,
        to_addrs=job.to,
        cc_addrs=job.cc,
        bcc_addrs=job.bcc,
        saved_path=saved_path,
    )
    tracker.set_last_run(job.name, now)
    log.info("sent %s (id=%s) to %s", job.name, send_id, ", ".join(job.to))
    return send_id


def run_due(
    config: Config,
    sender: Sender,
    tracker: Tracker,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[int]:
    """Send every job that is currently due. Returns the send-record ids.

    For a dry run nothing is persisted; the returned list is the names of the
    jobs that *would* have fired (as a count proxy).
    """
    now = now or datetime.now()
    due = due_jobs(config, tracker, now)
    if dry_run:
        for job in due:
            send_job(job, config, sender, tracker, now, dry_run=True)
        return list(range(len(due)))
    ids = []
    for job in due:
        send_id = send_job(job, config, sender, tracker, now)
        if send_id is not None:
            ids.append(send_id)
    return ids


def run_forever(
    config: Config,
    sender: Sender,
    tracker: Tracker,
    reply_check=None,
    stop=None,
) -> None:
    """Poll on ``config.poll_seconds`` and fire jobs as they come due.

    ``reply_check`` is an optional zero-arg callable run each cycle to refresh
    revert status. ``stop`` is an optional zero-arg predicate; when it returns
    True the loop exits (used by tests).
    """
    log.info("mailflow scheduler started (poll every %ss)", config.poll_seconds)
    while True:
        ids = run_due(config, sender, tracker)
        if ids:
            log.info("fired %d job(s)", len(ids))
        if reply_check is not None:
            try:
                reply_check()
            except Exception as exc:  # noqa: BLE001
                log.warning("reply check failed: %s", exc)
        if stop is not None and stop():
            break
        time.sleep(config.poll_seconds)
