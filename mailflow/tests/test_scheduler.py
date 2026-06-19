from datetime import datetime

from mailflow.config import Schedule
from mailflow.scheduler import next_run


def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm)


def test_interval_first_run_is_due_immediately():
    s = Schedule(every="interval", seconds=300)
    now = dt(2026, 6, 19, 12, 0)
    assert next_run(s, None, now) <= now


def test_interval_next_is_last_plus_period():
    s = Schedule(every="interval", seconds=600)
    last = dt(2026, 6, 19, 12, 0)
    assert next_run(s, last, dt(2026, 6, 19, 12, 5)) == dt(2026, 6, 19, 12, 10)


def test_daily_catches_up_missed_occurrence():
    s = Schedule(every="daily", at="08:00")
    # Never run, now is after 08:00 today -> due (catch-up).
    now = dt(2026, 6, 19, 9, 0)
    assert next_run(s, None, now) <= now
    # Never run, now is before 08:00 -> not due yet.
    now = dt(2026, 6, 19, 7, 0)
    assert next_run(s, None, now) > now


def test_daily_after_run_points_to_tomorrow():
    s = Schedule(every="daily", at="08:00")
    last = dt(2026, 6, 19, 8, 0)
    nxt = next_run(s, last, dt(2026, 6, 19, 9, 0))
    assert nxt == dt(2026, 6, 20, 8, 0)


def test_weekly_lands_on_weekday():
    s = Schedule(every="weekly", weekday=0, at="08:00")  # Monday
    # 2026-06-19 is a Friday; next Monday is 2026-06-22.
    nxt = next_run(s, dt(2026, 6, 15, 8, 0), dt(2026, 6, 19))
    assert nxt.weekday() == 0
    assert nxt == dt(2026, 6, 22, 8, 0)


def test_once_fires_once_then_never():
    s = Schedule(every="once", at="2026-06-19T08:00:00")
    now = dt(2026, 6, 19, 9, 0)
    assert next_run(s, None, now) <= now
    # After it has run, there is no next occurrence.
    assert next_run(s, dt(2026, 6, 19, 9, 0), now) is None
