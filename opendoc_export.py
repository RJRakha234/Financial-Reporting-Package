#!/usr/bin/env python3
"""
opendoc_export.py — download a SAP BusinessObjects Web Intelligence report as
Excel using the OpenDocument URL API.

This is the preferred way to automate a Web Intelligence (Webi) report: the
document, its output format and its prompt values all go into one URL, and the
server responds with the file. There is no UI to drive — no iframes, no prompt
dialog, no Export menu — so it does not break when the portal is patched.

How it authenticates
--------------------
BusinessObjects needs a logged-in session. Two ways, tried in this order:

  1. If BOE_COOKIES is set, those cookies are used directly.
  2. Otherwise Selenium opens the portal once so corporate SSO (or the logon
     form) can establish a session, then the session cookies are handed to
     `requests`, which streams the file down. The browser closes immediately
     after; the download itself never touches it.

Requirements
------------
    pip install requests
    pip install selenium        # only for the login step

Configuration (environment variables)
-------------------------------------
    BOE_BASE          BusinessObjects server origin, e.g.
                      https://boe.example.com   (required)
    BOE_DOC_CUID      The document CUID. Find it in the report's properties
                      panel, shown next to Name / ID.   (required)
    BOE_DOC_ID        Alternative to CUID — the numeric document ID.
    BOE_FORMAT        Output format. Default "E" (Excel).
                        E = Excel (.xls)   X = Excel 2007+ (.xlsx)
                        P = PDF            C = CSV (data)   H = HTML
    BOE_PROMPTS       Prompt values, semicolon-separated name=value pairs,
                      names exactly as shown in the Prompts dialog:
                        "Fiscal Year=2027;From Period=1;From To=3"
                      Multi-value prompts: separate values with a comma.
    BOE_OPENDOC_PATH  Override the OpenDocument path if your landscape uses a
                      non-default one. Default:
                        /BOE/OpenDocument/opendoc/openDocument.jsp
    BOE_COOKIES       Optional. Pre-supplied cookies as "name=value; name=value"
                      to skip the browser login entirely.
    SAP_PORTAL_BASE   Portal URL used for the SSO login step. Defaults to
                      BOE_BASE.
    SAP_USER/SAP_PASS Only needed if a logon form appears.
    BOE_DOWNLOAD_DIR  Where files are written. Default: ./sap_downloads

Run
---
    python opendoc_export.py
    python opendoc_export.py --output pl_2027_p1_p3.xls
    python opendoc_export.py --print-url     # show the URL and exit

Start with --print-url and paste that URL into a logged-in browser. If the
file downloads there, the script will work.
"""

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus

import requests

BOE_BASE = os.environ.get("BOE_BASE", "").rstrip("/")
DOC_CUID = os.environ.get("BOE_DOC_CUID", "")
DOC_ID = os.environ.get("BOE_DOC_ID", "")
FORMAT = os.environ.get("BOE_FORMAT", "E")
PROMPTS_RAW = os.environ.get("BOE_PROMPTS", "")
OPENDOC_PATH = os.environ.get(
    "BOE_OPENDOC_PATH", "/BOE/OpenDocument/opendoc/openDocument.jsp"
)
COOKIES_RAW = os.environ.get("BOE_COOKIES", "")
PORTAL_BASE = os.environ.get("SAP_PORTAL_BASE", "").rstrip("/") or BOE_BASE
SAP_USER = os.environ.get("SAP_USER", "")
SAP_PASS = os.environ.get("SAP_PASS", "")
DOWNLOAD_DIR = Path(os.environ.get("BOE_DOWNLOAD_DIR", "sap_downloads")).absolute()
LOGIN_TIMEOUT = int(os.environ.get("SAP_TIMEOUT", "90"))

# Extension per OpenDocument output format code.
FORMAT_SUFFIX = {"E": ".xls", "X": ".xlsx", "P": ".pdf", "C": ".csv", "H": ".html"}


def parse_prompts(raw: str) -> list:
    """"Name=value;Name2=v1,v2" -> [(name, [values]), ...]"""
    prompts = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        values = [v.strip() for v in value.split(",") if v.strip()]
        prompts.append((name.strip(), values))
    return prompts


def build_url() -> str:
    if not BOE_BASE:
        sys.exit("BOE_BASE is not set (the BusinessObjects server, e.g. "
                 "https://boe.example.com).")
    if not (DOC_CUID or DOC_ID):
        sys.exit("Set BOE_DOC_CUID (preferred) or BOE_DOC_ID — see the "
                 "report's properties panel.")

    params = []
    if DOC_CUID:
        params.append(("sIDType", "CUID"))
        params.append(("iDocID", DOC_CUID))
    else:
        params.append(("sIDType", "InfoObjectID"))
        params.append(("iDocID", DOC_ID))
    params.append(("sOutputFormat", FORMAT))
    # Refresh on open, so the returned file reflects the prompt values below
    # rather than whatever was cached with the document.
    params.append(("sRefresh", "Y"))

    query = "&".join(f"{k}={quote_plus(v)}" for k, v in params)

    # Prompts use the lsS<name> (single value) / lsM<name> (multi value)
    # convention, where <name> is the prompt text exactly as displayed.
    for name, values in parse_prompts(PROMPTS_RAW):
        prefix = "lsM" if len(values) > 1 else "lsS"
        query += f"&{prefix}{quote_plus(name)}={quote_plus(','.join(values))}"

    return f"{BOE_BASE}{OPENDOC_PATH}?{query}"


def cookies_from_string(raw: str) -> dict:
    jar = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            jar[name.strip()] = value.strip()
    return jar


def _fill_logon_form(driver) -> None:
    """Submit the portal/BOE logon form if one is displayed."""
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.common.by import By

    for ub, uv, pb, pv in [
        (By.ID, "logonuidfield", By.ID, "logonpassfield"),
        (By.NAME, "j_user", By.NAME, "j_password"),
        (By.NAME, "j_username", By.NAME, "j_password"),
    ]:
        try:
            user_field = driver.find_element(ub, uv)
            pass_field = driver.find_element(pb, pv)
        except NoSuchElementException:
            continue
        if not (SAP_USER and SAP_PASS):
            sys.exit("A logon form is shown but SAP_USER / SAP_PASS are not set.")
        user_field.send_keys(SAP_USER)
        pass_field.send_keys(SAP_PASS)
        pass_field.submit()
        return


def cookies_via_browser() -> dict:
    """Establish a session and return the BusinessObjects server's cookies.

    Two hops matter here. Signing in at the portal authenticates the user, but
    the document is served by the BOE server, which is usually a different
    host — and cookies are per-host. So after the portal we also load the BOE
    server, letting its SSO handoff run, and collect the cookies from there.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        sys.exit("selenium is required for the login step: pip install selenium")

    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    driver = webdriver.Edge(options=options)
    try:
        if PORTAL_BASE and PORTAL_BASE != BOE_BASE:
            print(f"Signing in via {PORTAL_BASE} ...")
            driver.get(PORTAL_BASE)
            _fill_logon_form(driver)
            WebDriverWait(driver, LOGIN_TIMEOUT).until(
                lambda d: len(d.get_cookies()) > 0
            )

        # Now the BusinessObjects host itself, so its session cookies exist.
        print(f"Establishing the BusinessObjects session at {BOE_BASE} ...")
        driver.get(f"{BOE_BASE}/BOE/BI")
        _fill_logon_form(driver)
        WebDriverWait(driver, LOGIN_TIMEOUT).until(
            lambda d: len(d.get_cookies()) > 0
        )

        jar = {c["name"]: c["value"] for c in driver.get_cookies()}
        print(f"Session established ({len(jar)} cookies on the BOE host).")
        return jar
    finally:
        driver.quit()


def download_via_browser(url: str, output: str | None) -> Path:
    """Fallback: let the browser fetch the document, exactly as a manual paste
    of the URL would. Slower than the direct request, but it inherits whatever
    authentication the browser already has."""
    try:
        from selenium import webdriver
    except ImportError:
        sys.exit("selenium is required for --browser: pip install selenium")
    import time

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    options.add_experimental_option("prefs", {
        "download.default_directory": str(DOWNLOAD_DIR),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
    })
    driver = webdriver.Edge(options=options)
    before = {p.name for p in DOWNLOAD_DIR.iterdir()}
    try:
        if PORTAL_BASE and PORTAL_BASE != BOE_BASE:
            driver.get(PORTAL_BASE)
            _fill_logon_form(driver)
        # The first OpenDocument request of a session redirects through logon
        # and drops the query parameters, so request it twice.
        driver.get(url)
        _fill_logon_form(driver)
        driver.get(url)

        deadline = time.time() + LOGIN_TIMEOUT * 4
        while time.time() < deadline:
            new = [
                p for p in DOWNLOAD_DIR.iterdir()
                if p.name not in before
                and p.suffix.lower() not in (".crdownload", ".tmp", ".partial")
            ]
            if new:
                newest = max(new, key=lambda p: p.stat().st_mtime)
                size = newest.stat().st_size
                time.sleep(2)
                if newest.stat().st_size == size and size > 0:
                    if output:
                        target = Path(output).absolute()
                        target.parent.mkdir(parents=True, exist_ok=True)
                        newest.replace(target)
                        newest = target
                    print(f"Downloaded: {newest}")
                    return newest
            time.sleep(1)
        sys.exit(f"No completed download appeared in {DOWNLOAD_DIR}.")
    finally:
        driver.quit()


def download(url: str, cookies: dict, output: str | None) -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = (
        Path(output).absolute()
        if output
        else DOWNLOAD_DIR / f"report{FORMAT_SUFFIX.get(FORMAT, '.bin')}"
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.cookies.update(cookies)

    print("Requesting the document ...")
    response = session.get(url, stream=True, timeout=600)
    response.raise_for_status()

    # The first OpenDocument request of a session redirects through logon and
    # drops the query parameters, so the reply is the viewer page rather than
    # a file. The redirect leaves a usable session behind, so asking again
    # normally succeeds — the same "paste it twice" behaviour seen manually.
    if "html" in response.headers.get("Content-Type", "").lower() and FORMAT != "H":
        print("Got the logon/viewer page; retrying now the session is warm ...")
        response.close()
        response = session.get(url, stream=True, timeout=600)
        response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    # Still HTML on the second attempt means something else is wrong — an
    # unanswered prompt, or rights that do not cover this document. Reporting
    # it beats writing a corrupt "Excel" file.
    if "html" in content_type.lower() and FORMAT != "H":
        preview = response.text[:400].replace("\n", " ")
        sys.exit(
            "The server returned an HTML page instead of a file. Usually a "
            "prompt has no value, or the session lacks rights on this "
            "document.\n"
            f"Content-Type: {content_type}\nFirst bytes: {preview}"
        )

    size = 0
    with open(target, "wb") as handle:
        for chunk in response.iter_content(65536):
            handle.write(chunk)
            size += len(chunk)
    if size == 0:
        sys.exit("The server returned an empty response.")
    print(f"Downloaded {size:,} bytes -> {target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a BusinessObjects Webi report via OpenDocument."
    )
    parser.add_argument("--output", "-o", help="Path to write the file to.")
    parser.add_argument(
        "--print-url", action="store_true",
        help="Print the OpenDocument URL and exit, without downloading. "
             "Paste it into a logged-in browser to verify it works.",
    )
    parser.add_argument(
        "--browser", action="store_true",
        help="Let the browser fetch the document instead of requesting it "
             "directly. Slower, but it inherits the browser's own "
             "authentication — useful if the direct request is refused.",
    )
    args = parser.parse_args()

    url = build_url()
    if args.print_url:
        print(url)
        return

    if args.browser:
        download_via_browser(url, args.output)
        return

    cookies = (
        cookies_from_string(COOKIES_RAW) if COOKIES_RAW else cookies_via_browser()
    )
    download(url, cookies, args.output)


if __name__ == "__main__":
    main()
