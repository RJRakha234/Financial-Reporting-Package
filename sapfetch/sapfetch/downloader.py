"""Orchestration: download a batch of reports for one period.

This is the public entry point most callers want::

    from sapfetch import load, Period, download_reports
    cfg = load("config.yaml")
    period = Period(fiscal_year=2025, from_period=1, to_period=10)
    results = download_reports(cfg, period)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config, ReportSpec
from .errors import SapfetchError
from .period import Period
from .portal import PortalSession
from .session import require_session


@dataclass
class DownloadResult:
    report: str
    ok: bool
    path: Path | None = None
    error: str | None = None     # populated when ok is False


def _safe(name: str) -> str:
    """Turn a report name into a filesystem-safe slug."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def output_path(config: Config, report: ReportSpec, period: Period) -> Path:
    """Compute the destination file for a report+period."""
    fmt = (report.export_format or config.defaults.export_format).lower()
    base = report.output_name or _safe(report.name)
    fname = f"{base}_{period.label}.{fmt}"
    return Path(config.defaults.download_dir) / fname


def prompt_values_for(
    config: Config, report: ReportSpec, period: Period
) -> dict[str, str]:
    """Combine period parameters with the report's static prompt overrides.

    Period values win for the period fields; static report/default prompts
    (e.g. a fixed consol group or currency) fill everything else.
    """
    values = dict(config.merged_prompts(report))           # defaults + report
    values.update(period.as_prompt_values(config.prompt_labels))  # period wins
    return values


def download_reports(
    config: Config,
    period: Period,
    only: list[str] | None = None,
) -> list[DownloadResult]:
    """Download every configured report (or just those named in ``only``).

    One report failing does not abort the batch — each result records its own
    success or error so a scheduled run can report partial success.
    """
    storage_state = require_session(config.portal)
    reports = config.reports
    if only:
        wanted = set(only)
        reports = [r for r in reports if r.name in wanted]
        missing = wanted - {r.name for r in reports}
        if missing:
            raise SapfetchError(
                f"unknown report(s): {', '.join(sorted(missing))}"
            )

    if not reports:
        return []  # nothing to do — don't spin up a browser

    results: list[DownloadResult] = []
    with PortalSession(config, storage_state) as portal:
        for report in reports:
            try:
                values = prompt_values_for(config, report, period)
                portal.open_report(report)
                portal.fill_prompts(values)
                portal.run()
                dest = output_path(config, report, period)
                portal.export(
                    report.export_format or config.defaults.export_format, dest
                )
                results.append(
                    DownloadResult(report.name, ok=True, path=dest)
                )
            except Exception as exc:  # capture, keep going
                results.append(
                    DownloadResult(report.name, ok=False, error=str(exc))
                )
    return results
