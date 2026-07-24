from datetime import datetime, timezone
from pathlib import Path

from reportbot.config import Config
from reportbot.mail.base import Message
from reportbot.runner import Runner
from reportbot.state import StateStore


class FakeMail:
    def __init__(self, messages):
        self.messages = messages

    def fetch_recent(self, folder, lookback_days):
        return list(self.messages)


def make_config(tmp_path):
    return Config.from_dict(
        {
            "download_dir": str(tmp_path / "dl"),
            "state_file": str(tmp_path / "state.json"),
            "triggers": [
                {
                    "name": "daily",
                    "from_contains": ["reports@portal.example.com"],
                    "subject_contains": ["Report ready"],
                }
            ],
        }
    )


def test_only_matching_messages_download(tmp_path):
    messages = [
        Message(
            id="match-1",
            sender="reports@portal.example.com",
            subject="Report ready for July",
            received=datetime(2026, 7, 24, tzinfo=timezone.utc),
        ),
        Message(
            id="nomatch-1",
            sender="newsletter@other.com",
            subject="Weekly digest",
            received=datetime(2026, 7, 24, 1, tzinfo=timezone.utc),
        ),
    ]
    downloaded = []

    def fake_download(message, rule):
        downloaded.append(message.id)
        return [Path("/tmp/fake-report.pdf")]

    runner = Runner(
        make_config(tmp_path),
        mail_client=FakeMail(messages),
        state=StateStore(tmp_path / "state.json"),
        download_fn=fake_download,
    )
    events = runner.run_once()

    assert downloaded == ["match-1"]
    assert len(events) == 1
    assert events[0].rule.name == "daily"


def test_dedupe_does_not_redownload(tmp_path):
    messages = [
        Message(
            id="match-1",
            sender="reports@portal.example.com",
            subject="Report ready",
            received=datetime(2026, 7, 24, tzinfo=timezone.utc),
        )
    ]
    calls = []
    state = StateStore(tmp_path / "state.json")

    def fake_download(message, rule):
        calls.append(message.id)
        return []

    def make():
        return Runner(
            make_config(tmp_path),
            mail_client=FakeMail(messages),
            state=state,
            download_fn=fake_download,
        )

    make().run_once()
    make().run_once()  # same message, second poll
    assert calls == ["match-1"]  # downloaded exactly once


def test_failed_download_is_retried_next_poll(tmp_path):
    messages = [
        Message(
            id="match-1",
            sender="reports@portal.example.com",
            subject="Report ready",
            received=datetime(2026, 7, 24, tzinfo=timezone.utc),
        )
    ]
    state = StateStore(tmp_path / "state.json")
    attempts = {"n": 0}

    def flaky_download(message, rule):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("portal down")
        return [Path("/tmp/ok.pdf")]

    def make():
        return Runner(
            make_config(tmp_path),
            mail_client=FakeMail(messages),
            state=state,
            download_fn=flaky_download,
        )

    first = make().run_once()
    assert first == []  # failed, nothing recorded
    assert not state.is_processed("match-1")

    second = make().run_once()
    assert len(second) == 1
    assert state.is_processed("match-1")
