import os

import pytest

from mailflow import load_config
from mailflow.config import ConfigError

GOOD = """
database: test.db
smtp:
  host: smtp.example.com
  port: 587
  security: starttls
  username: bot@example.com
  password_env: TEST_SMTP_PW
imap:
  host: imap.example.com
  port: 993
  security: ssl
  username: bot@example.com
  password_env: TEST_IMAP_PW
defaults:
  save_dir: out
  reply_window_days: 7
jobs:
  - name: weekly
    to: [a@example.com, b@example.com]
    cc: c@example.com
    subject: "Report {date}"
    body: hello
    attachments: docs/*.pdf
    schedule:
      every: weekly
      weekday: mon
      at: "08:00"
  - name: interval-job
    to: x@example.com
    subject: ping
    schedule:
      every: interval
      minutes: 5
"""


def write(tmp_path, text):
    p = tmp_path / "cfg.yaml"
    p.write_text(text)
    return p


def test_load_good_config(tmp_path):
    cfg = load_config(write(tmp_path, GOOD))
    assert cfg.database == "test.db"
    assert cfg.smtp.host == "smtp.example.com"
    assert cfg.smtp.sender_address() == "bot@example.com"
    assert cfg.imap is not None and cfg.imap.mailbox == "INBOX"
    assert len(cfg.jobs) == 2

    weekly = cfg.job("weekly")
    assert weekly.to == ["a@example.com", "b@example.com"]
    assert weekly.cc == ["c@example.com"]
    assert weekly.attachments == ["docs/*.pdf"]
    assert weekly.save_dir == "out"
    assert weekly.reply_window_days == 7
    assert weekly.schedule.every == "weekly"
    assert weekly.schedule.weekday == 0
    assert weekly.schedule.at == "08:00"

    interval = cfg.job("interval-job")
    assert interval.schedule.every == "interval"
    assert interval.schedule.seconds == 300


def test_password_from_env(tmp_path, monkeypatch):
    cfg = load_config(write(tmp_path, GOOD))
    monkeypatch.delenv("TEST_SMTP_PW", raising=False)
    with pytest.raises(ConfigError):
        cfg.smtp.password()
    monkeypatch.setenv("TEST_SMTP_PW", "secret")
    assert cfg.smtp.password() == "secret"


def test_missing_jobs_rejected(tmp_path):
    text = GOOD.split("jobs:")[0] + "jobs: []\n"
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_duplicate_job_names_rejected(tmp_path):
    text = """
smtp:
  host: h
  port: 25
  security: none
  username: u
  password_env: P
jobs:
  - {name: dup, to: a@e.com, subject: s}
  - {name: dup, to: b@e.com, subject: s}
"""
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_bad_time_rejected(tmp_path):
    text = """
smtp: {host: h, port: 25, security: none, username: u, password_env: P}
jobs:
  - name: j
    to: a@e.com
    subject: s
    schedule: {every: daily, at: "99:99"}
"""
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))
