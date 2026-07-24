"""Download report files from the portal.

Two modes, chosen automatically:

* **Direct link** — if the triggering email yields a URL (via
  ``portal.link_regex`` or any link in the body), the file is fetched straight
  from that URL inside an authenticated browser session.
* **Navigate** — otherwise the bot logs into ``login_url``, opens
  ``reports_url``, and clicks the configured ``download`` control.

Uses Playwright, imported lazily so importing the package never requires the
browser stack. Install with ``pip install playwright`` then
``playwright install chromium``.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import PortalConfig
    from .mail.base import Message


class PortalDownloader:
    def __init__(self, config: "PortalConfig"):
        self.config = config

    def _pick_link(self, message: "Message | None") -> str | None:
        if message is None:
            return None
        if self.config.link_regex:
            m = re.search(self.config.link_regex, message.body or "")
            if m:
                return m.group(1) if m.groups() else m.group(0)
            return None
        return message.links[0] if message.links else None

    def download(
        self, dest_dir: str | Path, message: "Message | None" = None
    ) -> list[Path]:
        """Download report file(s) into ``dest_dir``; return the saved paths."""
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "The portal downloader needs Playwright. Install it with: "
                "pip install playwright && playwright install chromium"
            ) from exc

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        cfg = self.config
        link = self._pick_link(message)
        saved: list[Path] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=cfg.headless)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(cfg.timeout_ms)
            try:
                if cfg.login_url:
                    self._login(page)

                if link:
                    with page.expect_download() as dl_info:
                        page.goto(link)
                    saved.append(self._save(dl_info.value, dest))
                else:
                    if cfg.reports_url:
                        page.goto(cfg.reports_url)
                    with page.expect_download() as dl_info:
                        page.click(cfg.selectors["download"])
                    saved.append(self._save(dl_info.value, dest))
            finally:
                context.close()
                browser.close()
        return saved

    def _login(self, page) -> None:
        cfg = self.config
        page.goto(cfg.login_url)
        sel = cfg.selectors
        if cfg.username and sel.get("username"):
            page.fill(sel["username"], cfg.username)
        if cfg.password and sel.get("password"):
            page.fill(sel["password"], cfg.password)
        if sel.get("submit"):
            page.click(sel["submit"])
            page.wait_for_load_state("networkidle")

    def _save(self, download, dest: Path) -> Path:
        suggested = download.suggested_filename or "report"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = dest / f"{stamp}-{suggested}"
        download.save_as(str(target))
        return target
