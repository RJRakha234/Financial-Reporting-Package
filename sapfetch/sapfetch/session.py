"""Authentication via a saved browser session.

The portal lives behind corporate SSO (``is.ad.infosys.com`` — ``ad`` = Active
Directory), so rather than scripting a fragile SSO/SAML form, we log in *once*
interactively and persist the authenticated cookies/localStorage with
Playwright's ``storage_state``. Every later headless download rides that state.

Playwright is imported lazily so the rest of the package (config, period) works
without a browser installed.
"""

from __future__ import annotations

from pathlib import Path

from .config import PortalConfig
from .errors import AuthError


def capture_session(portal: PortalConfig) -> str:
    """Open a visible browser at the portal, wait for the user to finish SSO,
    then save the authenticated session to ``portal.storage_state``.

    Returns the path the session was written to.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise AuthError(
            "Playwright is required: pip install playwright && playwright install chromium"
        ) from exc

    state_path = Path(portal.storage_state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Login is always interactive, so force a visible window regardless of the
    # configured headless flag, but still honour the browser channel/path.
    launch_kwargs = {**portal.launch_kwargs(), "headless": False}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(portal.base_url, timeout=portal.nav_timeout_ms)

        print(
            "\nA browser window has opened.\n"
            "  1. Complete the SSO login until you can see the reports portal.\n"
            "  2. Return here and press Enter to save the session.\n"
        )
        try:
            input("Press Enter once you are logged in... ")
        except (EOFError, KeyboardInterrupt):  # pragma: no cover
            browser.close()
            raise AuthError("session capture cancelled before login completed")

        context.storage_state(path=str(state_path))
        browser.close()
    return str(state_path)


def require_session(portal: PortalConfig) -> str:
    """Return the saved-session path, or raise if it does not exist yet."""
    state_path = Path(portal.storage_state)
    if not state_path.is_file():
        raise AuthError(
            f"no saved session at {state_path}. Run `sapfetch login` first."
        )
    return str(state_path)
