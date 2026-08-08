"""Configuration loading and validation."""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nifty50.config import Config, load_config, project_root
from nifty50.domain import Timeframe


def write_config(tmp_path: Path, mutate: object = None) -> Path:
    raw = yaml.safe_load((project_root() / "config.yaml").read_text(encoding="utf-8"))
    if callable(mutate):
        mutate(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


class TestLoading:
    def test_the_shipped_config_is_valid(self, real_config: Config) -> None:
        assert real_config.runtime.timezone == "Asia/Kolkata"
        assert real_config.data.base_timeframe is Timeframe.M15
        assert real_config.data.history_years == 7

    def test_paths_resolve_against_the_project_root(self, real_config: Config) -> None:
        holidays = real_config.path(real_config.calendar.holidays_file)
        assert holidays.is_absolute()
        assert holidays.exists()

    def test_every_referenced_reference_file_exists(self, real_config: Config) -> None:
        # Phase 1 owns these three. The constituents file arrives in Phase 2 and
        # is deliberately not asserted here.
        for relative in (
            real_config.calendar.holidays_file,
            real_config.calendar.special_sessions_file,
            real_config.corporate_actions.actions_file,
        ):
            assert real_config.path(relative).exists(), relative

    def test_historical_chunk_days_covers_every_configured_timeframe(
        self, real_config: Config
    ) -> None:
        caps = real_config.broker.kite.historical_chunk_days
        for timeframe in real_config.data.timeframes:
            assert timeframe in caps, f"no Kite chunk cap for {timeframe.value}"

    def test_session_windows_parse_as_times(self, real_config: Config) -> None:
        sessions = real_config.calendar.sessions
        assert sessions.continuous.start == dt.time(9, 15)
        assert sessions.continuous.end == dt.time(15, 30)


class TestValidation:
    def test_a_base_timeframe_outside_the_list_is_rejected(self, tmp_path: Path) -> None:
        def mutate(raw: dict) -> None:
            raw["data"]["base_timeframe"] = "1h"
            raw["data"]["timeframes"] = ["1m", "15m"]

        with pytest.raises(ValidationError, match=re.escape("not in data.timeframes")):
            load_config(write_config(tmp_path, mutate))

    def test_a_non_ist_timezone_is_rejected(self, tmp_path: Path) -> None:
        def mutate(raw: dict) -> None:
            raw["runtime"]["timezone"] = "UTC"

        with pytest.raises(ValidationError, match="Asia/Kolkata"):
            load_config(write_config(tmp_path, mutate))

    def test_an_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        # extra="forbid" everywhere: a typo'd key must fail at startup rather
        # than silently leaving the intended setting at its default.
        def mutate(raw: dict) -> None:
            raw["data"]["histry_years"] = 3

        with pytest.raises(ValidationError):
            load_config(write_config(tmp_path, mutate))

    def test_an_unknown_broker_is_rejected(self, tmp_path: Path) -> None:
        def mutate(raw: dict) -> None:
            raw["broker"]["name"] = "definitely-not-a-broker"

        with pytest.raises(ValidationError):
            load_config(write_config(tmp_path, mutate))

    def test_the_config_is_frozen(self, real_config: Config) -> None:
        with pytest.raises(ValidationError):
            real_config.data.history_years = 3  # type: ignore[misc]


class TestRegulatoryPosture:
    def test_the_config_declares_no_execution_surface(self) -> None:
        """PART 8: no order-related setting may exist anywhere in config.yaml."""
        raw = yaml.safe_load((project_root() / "config.yaml").read_text(encoding="utf-8"))
        forbidden = {"execution", "orders", "order", "trading", "broker_orders"}
        assert forbidden.isdisjoint(raw.keys())
        assert raw["runtime"]["mode"] in {"paper", "backtest"}


class TestDotenv:
    """Secrets live in .env, which only works if something reads the file.

    Nothing did. The Kite adapter writes its access token there and then looks
    for it in os.environ, so the documented "set it in .env and re-run" loop
    wrote to a file no process ever opened.
    """

    def test_dot_env_reaches_the_environment(self, tmp_path, monkeypatch) -> None:
        from nifty50.config import load_env

        monkeypatch.delenv("KITE_API_KEY", raising=False)
        (tmp_path / ".env").write_text("KITE_API_KEY=abc123\n", encoding="utf-8")
        assert load_env(tmp_path) == tmp_path / ".env"
        assert os.environ["KITE_API_KEY"] == "abc123"

    def test_a_real_environment_variable_wins(self, tmp_path, monkeypatch) -> None:
        """An exported value or one injected by CI is a deliberate act; a stale
        line in a working-copy .env must not silently override it."""
        from nifty50.config import load_env

        monkeypatch.setenv("KITE_API_KEY", "from-the-shell")
        (tmp_path / ".env").write_text("KITE_API_KEY=from-the-file\n", encoding="utf-8")
        load_env(tmp_path)
        assert os.environ["KITE_API_KEY"] == "from-the-shell"

    def test_no_dot_env_is_not_an_error(self, tmp_path) -> None:
        from nifty50.config import load_env

        assert load_env(tmp_path) is None

    def test_loading_config_loads_the_env_beside_it(self, tmp_path, monkeypatch) -> None:
        from nifty50.config import load_config, project_root

        monkeypatch.delenv("KITE_API_KEY", raising=False)
        (tmp_path / ".env").write_text("KITE_API_KEY=beside-the-config\n", encoding="utf-8")
        shutil.copy(project_root() / "config.yaml", tmp_path / "config.yaml")
        load_config(tmp_path / "config.yaml")
        assert os.environ["KITE_API_KEY"] == "beside-the-config"


def test_the_example_env_lists_every_variable_the_code_reads() -> None:
    """A credential the code needs but the example omits is a setup step
    nobody discovers until the traceback."""
    from nifty50.config import project_root

    example = (project_root() / ".env.example").read_text(encoding="utf-8")
    settings = load_config().broker.kite
    for name in (
        settings.api_key_env, settings.api_secret_env,
        settings.access_token_env, settings.request_token_env,
    ):
        assert f"{name}=" in example, f"{name} missing from .env.example"
