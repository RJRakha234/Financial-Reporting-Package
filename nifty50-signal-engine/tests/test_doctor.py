"""The setup diagnostic.

Everything here is a failure that used to surface as a stack trace pointing
at the wrong thing. The BOM case is the reason the script exists: Notepad's
default save silently detaches the first key in the file, so the credential
sitting in plain sight reports as "not set".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nifty50.config import load_env, read_text_any_encoding
from nifty50.scripts.doctor import main


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):  # type: ignore[no-untyped-def]
    for name in (
        "KITE_API_KEY", "KITE_API_SECRET", "KITE_ACCESS_TOKEN", "KITE_REQUEST_TOKEN"
    ):
        monkeypatch.delenv(name, raising=False)


class TestEncodings:
    """What editors actually write, as opposed to what they ought to."""

    @pytest.mark.parametrize(
        "name,raw",
        [
            ("plain utf-8", b"KITE_API_KEY=abc\n"),
            ("utf-8 with BOM", b"\xef\xbb\xbfKITE_API_KEY=abc\n"),
            ("utf-16 le", "KITE_API_KEY=abc\n".encode("utf-16")),
            ("utf-16 be", b"\xfe\xff" + "KITE_API_KEY=abc\n".encode("utf-16-be")),
            ("crlf", b"KITE_API_KEY=abc\r\n"),
            ("quoted", b'KITE_API_KEY="abc"\n'),
            ("padded", b"KITE_API_KEY = abc\n"),
        ],
    )
    def test_the_first_key_survives(self, tmp_path: Path, name: str, raw: bytes) -> None:
        """A BOM attaches to the first key, so KITE_API_KEY -- the first line
        of .env.example -- is precisely the one that goes missing."""
        (tmp_path / ".env").write_bytes(raw)
        load_env(tmp_path)
        assert os.environ.get("KITE_API_KEY") == "abc", name

    def test_undecodable_bytes_do_not_raise(self, tmp_path: Path) -> None:
        """Windows ANSI smart quotes in a comment must not take the process
        down before it reaches the credential three lines below."""
        (tmp_path / ".env").write_bytes(b"# \x93note\x94\nKITE_API_KEY=abc\n")
        load_env(tmp_path)
        assert os.environ.get("KITE_API_KEY") == "abc"

    def test_a_utf32_file_is_not_read_as_utf16(self, tmp_path: Path) -> None:
        """UTF-16's BOM is a prefix of UTF-32's; testing short-first decodes
        the file as the wrong encoding and yields plausible rubbish."""
        path = tmp_path / "x.txt"
        path.write_bytes("KITE_API_KEY=abc\n".encode("utf-32"))
        assert read_text_any_encoding(path).strip() == "KITE_API_KEY=abc"


class TestDoctor:
    def test_a_missing_env_file_fails_with_the_copy_command(self, tmp_path, capsys) -> None:
        assert main([]) == 1
        out = capsys.readouterr().out
        assert "copy .env.example .env" in out

    def test_placeholder_text_is_caught_rather_than_sent_to_the_broker(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """<the access_token from .kite_session.json> is a value someone
        pasted verbatim. Sending it to Kite yields an opaque 403."""
        monkeypatch.setenv("KITE_API_KEY", "r7x5c8mansiiw6hl")
        monkeypatch.setenv("KITE_API_SECRET", "<the secret>")
        main([])
        out = capsys.readouterr().out
        assert "still the placeholder text" in out

    def test_it_never_prints_a_credential(self, monkeypatch, capsys) -> None:
        """The output is meant to be safe to paste into a chat window."""
        secret = "s3cr3t_value_do_not_leak"
        monkeypatch.setenv("KITE_API_KEY", "abcdefghijklmnop")
        monkeypatch.setenv("KITE_API_SECRET", secret)
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "tok_" + secret)
        main([])
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

    def test_a_complete_setup_passes(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("KITE_API_KEY", "r7x5c8mansiiw6hl")
        monkeypatch.setenv("KITE_API_SECRET", "abcdefghijklmnopqrst")
        monkeypatch.setenv("KITE_ACCESS_TOKEN", "abcdefghijklmnopqrst")
        assert main([]) == 0
        assert "Setup looks good." in capsys.readouterr().out

    def test_comment_lines_are_not_counted_as_index_members(self, capsys) -> None:
        """The shipped constituents file is 56 lines of explanation and no
        data. Counting lines would report a populated universe and hide the
        gap that invalidates every cross-sectional result in the project."""
        monkeypatch_free = main([])
        assert monkeypatch_free in (0, 1)
        assert "0 row(s) in nifty50_constituents.csv" in capsys.readouterr().out
