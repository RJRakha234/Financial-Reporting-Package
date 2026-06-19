from datetime import datetime

from mailflow import DryRunSender, Tracker, run_due, send_job
from mailflow.config import Config, Job, Schedule, SmtpConfig


def make_config(tmp_path, schedule=None, track=False):
    smtp = SmtpConfig(
        host="h", port=25, security="none", username="bot@corp.com", password_env="X"
    )
    job = Job(
        name="weekly",
        to=["a@corp.com"],
        cc=["c@corp.com"],
        subject="Report {date}",
        body="hi",
        schedule=schedule,
        track_replies=track,
    )
    return Config(
        smtp=smtp,
        imap=None,
        jobs=[job],
        database=str(tmp_path / "t.db"),
        save_dir=str(tmp_path / "out"),
    )


def test_send_job_records_and_archives(tmp_path):
    cfg = make_config(tmp_path)
    sender = DryRunSender()
    with Tracker(cfg.database) as tracker:
        send_id = send_job(cfg.job("weekly"), cfg, sender, tracker)
        rec = tracker.find_by_id(send_id)
    assert rec.status == "sent"
    assert rec.message_id and rec.message_id.endswith("@corp.com>")
    assert rec.saved_path and rec.saved_path.endswith(".eml")
    assert sender.sent == [(rec.subject, ["a@corp.com", "c@corp.com"])]


def test_send_failure_recorded(tmp_path):
    cfg = make_config(tmp_path)

    class Boom:
        def send(self, msg, recipients):
            raise RuntimeError("smtp down")

    with Tracker(cfg.database) as tracker:
        send_id = send_job(cfg.job("weekly"), cfg, Boom(), tracker)
        rec = tracker.find_by_id(send_id)
    assert rec.status == "failed"
    assert "smtp down" in rec.error


def test_run_due_fires_only_due_jobs(tmp_path):
    cfg = make_config(tmp_path, schedule=Schedule(every="interval", seconds=3600))
    sender = DryRunSender()
    with Tracker(cfg.database) as tracker:
        first = run_due(cfg, sender, tracker)
        # Immediately after, the interval job is not due again.
        second = run_due(cfg, sender, tracker)
    assert len(first) == 1
    assert second == []


def test_mark_replied_and_counts(tmp_path):
    cfg = make_config(tmp_path, track=True)
    with Tracker(cfg.database) as tracker:
        send_id = send_job(cfg.job("weekly"), cfg, DryRunSender(), tracker)
        assert tracker.pending_reply_sends()
        assert tracker.mark_replied(send_id, reply_from="a@corp.com")
        # Marking again is a no-op.
        assert not tracker.mark_replied(send_id)
        rec = tracker.find_by_id(send_id)
        assert rec.status == "replied"
        assert rec.reply_from == "a@corp.com"
        assert tracker.counts().get("replied") == 1
        assert tracker.pending_reply_sends() == []


def test_last_run_roundtrip(tmp_path):
    cfg = make_config(tmp_path)
    with Tracker(cfg.database) as tracker:
        assert tracker.last_run("weekly") is None
        when = datetime(2026, 6, 19, 8, 0)
        tracker.set_last_run("weekly", when)
        assert tracker.last_run("weekly") == when
