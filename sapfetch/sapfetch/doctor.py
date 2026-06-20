"""Environment preflight checks — ``sapfetch doctor``.

In an air-gapped box you cannot pip-install on the fly or watch a browser fail,
so this verifies every moving part up front and prints a ✓/✗ checklist:

  * Python version
  * playwright / PyYAML importable
  * the configured browser actually launches (bundled Chromium or corporate
    Edge/Chrome via channel/executable_path)
  * the config file parses
  * a saved SSO session exists
  * (optional, --check-portal) the portal URL is reachable with that session

Each check is independent and never raises; failures are collected and the
overall result drives the exit code.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .config import Config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _check_python() -> Check:
    v = sys.version_info
    ok = (v.major, v.minor) >= (3, 10)
    return Check(
        "Python >= 3.10",
        ok,
        f"found {v.major}.{v.minor}.{v.micro}"
        + ("" if ok else " — sapfetch needs 3.10+"),
    )


def _check_import(module: str, hint: str) -> Check:
    try:
        __import__(module)
        return Check(f"import {module}", True)
    except Exception as exc:  # noqa: BLE001 - report any import failure
        return Check(f"import {module}", False, f"{exc} ({hint})")


def _check_browsers_path() -> Check:
    path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not path:
        return Check(
            "PLAYWRIGHT_BROWSERS_PATH",
            True,
            "not set — using Playwright's default cache location",
        )
    exists = os.path.isdir(path)
    return Check("PLAYWRIGHT_BROWSERS_PATH", exists,
                 f"{path}" + ("" if exists else " — directory not found"))


def _check_browser_launch(config: Config) -> Check:
    """Actually launch and close the configured browser — the real test."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return Check("browser launches", False, f"playwright missing: {exc}")

    kwargs = config.portal.launch_kwargs()
    using = (
        f"channel={config.portal.browser_channel}"
        if config.portal.browser_channel
        else (
            f"executable={config.portal.executable_path}"
            if config.portal.executable_path
            else "bundled Chromium"
        )
    )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**{**kwargs, "headless": True})
            browser.close()
        return Check("browser launches", True, using)
    except Exception as exc:  # noqa: BLE001
        return Check("browser launches", False, f"{using}: {exc}")


def _check_session(config: Config) -> Check:
    from pathlib import Path

    p = Path(config.portal.storage_state)
    if p.is_file():
        return Check("saved SSO session", True, str(p))
    return Check("saved SSO session", False,
                 f"{p} not found — run `sapfetch login`")


def _check_portal(config: Config) -> Check:
    """Best-effort: open the portal with the saved session (needs network)."""
    from pathlib import Path

    if not Path(config.portal.storage_state).is_file():
        return Check("portal reachable", False, "no session to test with")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                **{**config.portal.launch_kwargs(), "headless": True}
            )
            ctx = browser.new_context(storage_state=config.portal.storage_state)
            page = ctx.new_page()
            resp = page.goto(
                config.portal.base_url, timeout=config.portal.nav_timeout_ms
            )
            status = resp.status if resp else "?"
            browser.close()
        ok = resp is not None and resp.ok
        return Check("portal reachable", ok,
                     f"{config.portal.base_url} -> HTTP {status}")
    except Exception as exc:  # noqa: BLE001
        return Check("portal reachable", False, str(exc))


def run_doctor(config: Config, check_portal: bool = False) -> list[Check]:
    checks = [
        _check_python(),
        _check_import("playwright", "pip install playwright"),
        _check_import("yaml", "pip install pyyaml"),
        _check_browsers_path(),
        _check_browser_launch(config),
        _check_session(config),
    ]
    if check_portal:
        checks.append(_check_portal(config))
    return checks
