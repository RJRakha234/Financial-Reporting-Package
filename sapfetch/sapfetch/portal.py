"""The browser engine: open a report, fill the period prompts, export the file.

This is the only module that touches the live DOM, so all of its selectors come
from :class:`~sapfetch.config.Selectors` (calibrate once in YAML). The flow per
report is:

    open_report(path) -> fill_prompts(values) -> run() -> export(format) -> file

SAP Web Intelligence usually renders inside an iframe; :meth:`scope` resolves the
right frame (configurable via ``portal.report_frame``) and everything operates
within it.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config, PortalConfig, ReportSpec, Selectors
from .errors import ExportError, NavigationError, PromptError, SapfetchError


class PortalSession:
    """A live, authenticated portal session driving one browser context."""

    def __init__(self, config: Config, storage_state: str):
        self._config = config
        self._portal: PortalConfig = config.portal
        self._selectors: Selectors = config.selectors
        self._storage_state = storage_state
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # -- context-manager lifecycle ----------------------------------------
    def __enter__(self) -> "PortalSession":
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise SapfetchError(
                "Playwright is required: pip install playwright "
                "&& playwright install chromium"
            ) from exc

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                **self._portal.launch_kwargs()
            )
            self._context = self._browser.new_context(
                storage_state=self._storage_state, accept_downloads=True
            )
            self._context.set_default_timeout(self._portal.action_timeout_ms)
            self._page = self._context.new_page()
            self._page.goto(
                self._portal.base_url, timeout=self._portal.nav_timeout_ms
            )
        except Exception as exc:
            # Never leave the Playwright driver running on a partial start.
            self.__exit__(None, None, None)
            if isinstance(exc, SapfetchError):
                raise
            raise SapfetchError(
                f"could not start the browser session: {exc}"
            ) from exc
        return self

    def __exit__(self, *exc) -> None:
        for closer in (self._browser, self._pw):
            try:
                if closer is None:
                    continue
                if closer is self._pw:
                    closer.stop()
                else:
                    closer.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._pw = self._browser = self._context = self._page = None

    # A short, capped settle: Playwright already auto-waits before every click
    # and fill, so this is just a brief pause for in-flight requests — never the
    # full action timeout (SAP portals poll/hold sockets and may never idle).
    _SETTLE_CAP_MS = 5_000

    def _settle(self) -> None:
        """Best-effort, time-capped wait for the page to quiesce."""
        timeout = min(self._portal.action_timeout_ms, self._SETTLE_CAP_MS)
        try:
            self._page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass

    # -- frame resolution -------------------------------------------------
    def scope(self):
        """Return the page or iframe the report content lives in."""
        page = self._page
        hint = self._portal.report_frame
        if not hint:
            return page
        for frame in page.frames:
            if hint in (frame.url or "") or hint == (frame.name or ""):
                return frame
        # Fall back to the main frame rather than failing hard.
        return page

    # -- navigation -------------------------------------------------------
    def open_report(self, report: ReportSpec) -> None:
        """Walk ``report.open_path`` to open the report.

        Always starts from the portal home so each report in a batch is opened
        independently of where the previous one left the page.
        """
        page = self._page
        try:
            page.goto(self._portal.base_url, timeout=self._portal.nav_timeout_ms)
            self._settle()
        except Exception:
            # If we cannot reach home, the nav-item clicks below report the
            # real, more specific failure.
            pass

        sel = self._selectors.nav_item
        for step in report.open_path:
            locator = page.locator(sel.format(step=step)).first
            try:
                locator.wait_for(state="visible",
                                 timeout=self._portal.action_timeout_ms)
                locator.click()
            except Exception as exc:
                raise NavigationError(
                    f"could not click {step!r} while opening "
                    f"{report.name!r}: {exc}"
                ) from exc
            self._settle()

    # -- prompts ----------------------------------------------------------
    def fill_prompts(self, values: dict[str, str]) -> None:
        """Enter each ``{label: value}`` into the prompt panel."""
        scope = self.scope()
        for label, value in values.items():
            try:
                self._set_one_prompt(scope, label, value)
            except Exception as exc:
                raise PromptError(
                    f"could not set prompt {label!r}={value!r}: {exc}"
                ) from exc

    def _set_one_prompt(self, scope, label: str, value: str) -> None:
        row = scope.locator(
            self._selectors.prompt_row_by_label.format(label=label)
        ).last
        row.wait_for(state="visible", timeout=self._portal.action_timeout_ms)
        row.click()
        field = row.locator(self._selectors.prompt_input).first
        if field.count() == 0:
            # Some layouts put the input as a sibling, not a descendant.
            field = scope.locator(self._selectors.prompt_input).first
        field.click()
        field.fill(value)  # fill() replaces any existing content
        field.press("Enter")

    def run(self) -> None:
        """Submit the prompts and run the report."""
        scope = self.scope()
        btn = scope.locator(self._selectors.run_button).first
        try:
            btn.wait_for(state="visible", timeout=self._portal.action_timeout_ms)
            btn.click()
        except Exception as exc:
            raise NavigationError(f"could not run the report: {exc}") from exc
        self._settle()

    # -- export -----------------------------------------------------------
    def export(self, fmt: str, dest: Path) -> Path:
        """Export the currently-open report to ``dest`` and return the path."""
        scope = self.scope()
        page = self._page
        try:
            scope.locator(self._selectors.export_button).first.click()
            option = scope.locator(
                self._selectors.export_format_option.format(format=fmt.upper())
            ).first
            if option.count():
                option.click()
            with page.expect_download(
                timeout=self._portal.nav_timeout_ms
            ) as dl_info:
                scope.locator(self._selectors.export_confirm).first.click()
            download = dl_info.value
        except Exception as exc:
            raise ExportError(
                f"export to {fmt} failed: {exc}"
            ) from exc

        dest.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(str(dest))
        return dest
