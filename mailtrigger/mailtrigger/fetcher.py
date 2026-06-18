"""Fetch the report from a URL using the configured parameters."""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

from .mailbox import Email


@dataclass
class FetchResult:
    content: bytes
    content_type: str
    url: str


def _resolve_url(report: dict, mail: Email | None) -> str:
    pattern = (report.get("url_from_email_regex") or "").strip()
    if pattern and mail is not None:
        m = re.search(pattern, mail.body)
        if m:
            return m.group(0)
    url = (report.get("url") or "").strip()
    if not url:
        raise ValueError(
            "no report URL: set report.url, or report.url_from_email_regex "
            "with a link present in the email body"
        )
    return url


def fetch(report: dict, mail: Email | None = None) -> FetchResult:
    url = _resolve_url(report, mail)
    method = (report.get("method") or "GET").upper()
    params = report.get("params") or {}
    headers = report.get("headers") or {}
    timeout = float(report.get("timeout_seconds", 60))

    if method == "GET":
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    elif method == "POST":
        resp = requests.post(url, data=params, headers=headers, timeout=timeout)
    else:
        raise ValueError(f"unsupported report.method: {method!r} (use GET or POST)")

    resp.raise_for_status()
    return FetchResult(
        content=resp.content,
        content_type=resp.headers.get("Content-Type", ""),
        url=resp.url,
    )
