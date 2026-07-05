#!/usr/bin/env python3
"""
sap_export.py — download a BEx Web report from the SAP NetWeaver (/irj) portal
as an Excel file, using Selenium.

Built for the "Real time Company-wise Trial balance" report (BW/4HANA BEx Web
report embedded in the portal), but the selectors are generic enough for most
BEx Web reports served through /irj/portal.

Why this design
---------------
The BEx report lives two iframes deep (contentAreaFrame -> isolatedWorkArea),
which is why a naive driver.find_element() never sees the Export button. This
script walks the frames explicitly, runs the variable screen, clicks
"Export to Microsoft Excel", and waits for the file to land in the download
directory. The actual file transfer happens in a popup the server opens, so we
never need to touch the popup itself — the browser saves the file for us.

Setup (on the machine with browser access to the portal)
--------------------------------------------------------
    pip install selenium
    # Selenium 4.6+ downloads the Edge/Chrome driver automatically.

Configuration is via environment variables so no credentials live in code:

    SAP_REPORT_URL   URL of the report. Open the report in the portal until it
                     is in its own tab (or copy the iframe URL), then copy the
                     address bar. Required.
    SAP_USER         Portal user. Optional — omit if the portal signs you in
                     via corporate SSO without a form.
    SAP_PASS         Portal password. Optional, same as above.
    SAP_DOWNLOAD_DIR Where the Excel file should be saved.
                     Default: ./sap_downloads
    SAP_BROWSER      "edge" (default) or "chrome".
    SAP_TIMEOUT      Seconds to wait for slow portal pages. Default 90.

Run:
    python sap_export.py
    python sap_export.py --output trial_balance.xls

Exit status 0 and the saved file path on success.
"""

import argparse
import os
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPORT_URL = os.environ.get("SAP_REPORT_URL", "")
SAP_USER = os.environ.get("SAP_USER", "")
SAP_PASS = os.environ.get("SAP_PASS", "")
DOWNLOAD_DIR = Path(os.environ.get("SAP_DOWNLOAD_DIR", "sap_downloads")).absolute()
BROWSER = os.environ.get("SAP_BROWSER", "edge").lower()
TIMEOUT = int(os.environ.get("SAP_TIMEOUT", "90"))

# The two frames the portal wraps BEx reports in. If SAP renames them in a
# future patch, the recursive fallback below still finds the report.
KNOWN_FRAME_PATH = ["contentAreaFrame", "isolatedWorkArea"]

EXPORT_BUTTON_XPATHS = [
    "//*[normalize-space(text())='Export to Microsoft Excel']",
    "//*[contains(@title,'Export to Microsoft Excel')]",
    "//*[contains(@title,'Export') and contains(@title,'Excel')]",
]

OK_BUTTON_XPATHS = [
    "//input[@type='submit' and @value='OK']",
    "//input[@type='button' and @value='OK']",
    "//*[@title='OK']",
    "//*[normalize-space(text())='OK' and (self::a or self::div or self::span or self::button)]",
]

EXCEL_EXTENSIONS = (".xlsx", ".xls", ".mhtml", ".mht", ".csv")
PARTIAL_EXTENSIONS = (".crdownload", ".tmp", ".partial")


# ---------------------------------------------------------------------------
# Browser setup
# ---------------------------------------------------------------------------

def build_driver() -> webdriver.Remote:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    prefs = {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    if BROWSER == "chrome":
        options = webdriver.ChromeOptions()
        options.add_experimental_option("prefs", prefs)
        options.add_argument("--start-maximized")
        return webdriver.Chrome(options=options)
    options = webdriver.EdgeOptions()
    options.add_experimental_option("prefs", prefs)
    options.add_argument("--start-maximized")
    return webdriver.Edge(options=options)


# ---------------------------------------------------------------------------
# Login (only if the portal shows a form; corporate SSO may skip it entirely)
# ---------------------------------------------------------------------------

def maybe_login(driver: webdriver.Remote) -> None:
    """Fill the NetWeaver portal logon form if one is displayed."""
    # Standard NetWeaver portal logon field ids/names, then generic fallbacks.
    candidates = [
        (By.ID, "logonuidfield", By.ID, "logonpassfield"),
        (By.NAME, "j_user", By.NAME, "j_password"),
        (By.NAME, "j_username", By.NAME, "j_password"),
        (By.CSS_SELECTOR, "input[type='text']", By.CSS_SELECTOR, "input[type='password']"),
    ]
    for ub, uv, pb, pv in candidates:
        try:
            user_field = driver.find_element(ub, uv)
            pass_field = driver.find_element(pb, pv)
        except NoSuchElementException:
            continue
        if not (SAP_USER and SAP_PASS):
            sys.exit(
                "The portal is showing a login form but SAP_USER / SAP_PASS "
                "are not set. Set them as environment variables and rerun."
            )
        user_field.clear()
        user_field.send_keys(SAP_USER)
        pass_field.clear()
        pass_field.send_keys(SAP_PASS)
        pass_field.submit()
        print("Submitted portal login form.")
        return
    print("No login form detected (SSO or already signed in).")


# ---------------------------------------------------------------------------
# Frame handling
# ---------------------------------------------------------------------------

def find_element_any(driver, xpaths):
    for xp in xpaths:
        try:
            el = driver.find_element(By.XPATH, xp)
            if el.is_displayed():
                return el
        except (NoSuchElementException, WebDriverException):
            continue
    return None


def switch_into_report(driver, xpaths, deadline) -> "webdriver.remote.webelement.WebElement":
    """Focus the frame containing any of `xpaths` and return that element.

    Tries the known portal frame path first, then a recursive search of every
    frame in the page. Retries until `deadline` because portal frames appear
    late.
    """
    while time.time() < deadline:
        # Attempt 1: the frame names we saw in this portal.
        driver.switch_to.default_content()
        try:
            for name in KNOWN_FRAME_PATH:
                WebDriverWait(driver, 5).until(
                    EC.frame_to_be_available_and_switch_to_it((By.ID, name))
                )
            el = find_element_any(driver, xpaths)
            if el:
                return el
        except TimeoutException:
            pass

        # Attempt 2: brute-force walk of the whole frame tree.
        driver.switch_to.default_content()
        el = _search_frames(driver, xpaths, depth=0, max_depth=6)
        if el:
            return el
        time.sleep(2)
    raise TimeoutException(f"Element not found in any frame: {xpaths[0]} ...")


def _search_frames(driver, xpaths, depth, max_depth):
    el = find_element_any(driver, xpaths)
    if el:
        return el
    if depth >= max_depth:
        return None
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for i in range(len(frames)):
        # Re-fetch each time: the DOM may have changed while we were away.
        frames_now = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
        if i >= len(frames_now):
            break
        try:
            driver.switch_to.frame(frames_now[i])
        except WebDriverException:
            continue
        found = _search_frames(driver, xpaths, depth + 1, max_depth)
        if found:
            return found  # stay focused in the frame that contains it
        driver.switch_to.parent_frame()
    return None


# ---------------------------------------------------------------------------
# Report flow
# ---------------------------------------------------------------------------

def run_variable_screen(driver, deadline) -> None:
    """Click OK on the BEx variable screen if it appears.

    The screen keeps whatever selection was last used (e.g. fiscal period
    000.2023 - 012.2023), so confirming with OK is normally all that is
    needed. If the report has no variable screen this simply times out
    quickly and we move on.
    """
    try:
        ok = switch_into_report(driver, OK_BUTTON_XPATHS, min(deadline, time.time() + 30))
    except TimeoutException:
        print("No variable screen shown — continuing.")
        return
    ok.click()
    print("Variable screen confirmed (OK).")


def snapshot(directory: Path):
    return {p.name for p in directory.iterdir()} if directory.exists() else set()


def wait_for_download(directory: Path, before: set, deadline: float) -> Path:
    """Wait until a new, fully-written Excel-ish file appears."""
    while time.time() < deadline:
        candidates = [
            p for p in directory.iterdir()
            if p.name not in before and p.suffix.lower() in EXCEL_EXTENSIONS
        ]
        partials = [
            p for p in directory.iterdir()
            if p.name not in before and p.suffix.lower() in PARTIAL_EXTENSIONS
        ]
        if candidates and not partials:
            newest = max(candidates, key=lambda p: p.stat().st_mtime)
            size = newest.stat().st_size
            time.sleep(2)  # confirm the size is stable
            if newest.stat().st_size == size and size > 0:
                return newest
        time.sleep(1)
    raise TimeoutException(
        f"No completed Excel download appeared in {directory} "
        f"(check the browser window for an error or a blocked popup)."
    )


def export_report(output: str | None) -> Path:
    if not REPORT_URL:
        sys.exit(
            "SAP_REPORT_URL is not set. Open the report in the portal, copy "
            "the address-bar URL of the report tab, and set SAP_REPORT_URL."
        )
    driver = build_driver()
    deadline = time.time() + TIMEOUT * 4  # overall budget for the whole flow
    try:
        print(f"Opening report: {REPORT_URL}")
        driver.get(REPORT_URL)
        maybe_login(driver)

        run_variable_screen(driver, deadline)

        print("Waiting for the report toolbar ...")
        export_btn = switch_into_report(driver, EXPORT_BUTTON_XPATHS, deadline)

        before = snapshot(DOWNLOAD_DIR)
        main_window = driver.current_window_handle
        export_btn.click()
        print("Clicked 'Export to Microsoft Excel' — waiting for the file ...")

        file_path = wait_for_download(DOWNLOAD_DIR, before, deadline)

        # Close the export popup if the server opened one.
        for handle in driver.window_handles:
            if handle != main_window:
                driver.switch_to.window(handle)
                driver.close()
        driver.switch_to.window(main_window)

        if output:
            target = Path(output).absolute()
            target.parent.mkdir(parents=True, exist_ok=True)
            file_path.replace(target)
            file_path = target
        print(f"Downloaded: {file_path}")
        return file_path
    finally:
        driver.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--output", "-o",
        help="Move the downloaded file to this path (default: keep the "
             "browser's file name inside SAP_DOWNLOAD_DIR).",
    )
    args = parser.parse_args()
    export_report(args.output)


if __name__ == "__main__":
    main()
