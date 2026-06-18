"""Load and validate the YAML config, expanding ${ENV_VAR} references."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand(value: Any) -> Any:
    """Recursively replace ${VAR} with os.environ['VAR'] in strings."""
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            if var not in os.environ:
                raise KeyError(
                    f"config references ${{{var}}} but that environment "
                    f"variable is not set"
                )
            return os.environ[var]

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    cfg = _expand(raw)

    for section in ("mailbox", "trigger", "report", "output"):
        if section not in cfg:
            raise ValueError(f"config is missing the required '{section}' section")
    return cfg
