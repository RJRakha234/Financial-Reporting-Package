"""sapfetch — automate downloading SAP BI / Group Reporting reports.

Logs in to the SAP NetWeaver Enterprise Portal once via SSO, then pulls one or
more Web Intelligence reports (e.g. the GR INDAS / IFRS INR / IFRS USD
consolidated P&Ls) for a chosen reporting period, and can hand each file to
``fincheck`` for a footing check.

Public API::

    from sapfetch import load, Period, download_reports
    cfg = load("config.yaml")
    results = download_reports(cfg, Period(2025, 1, 10))
"""

from .config import Config, ReportSpec, load, from_dict
from .downloader import DownloadResult, download_reports, output_path
from .errors import SapfetchError
from .period import Period

__version__ = "0.1.0"

__all__ = [
    "Config",
    "ReportSpec",
    "Period",
    "DownloadResult",
    "download_reports",
    "output_path",
    "load",
    "from_dict",
    "SapfetchError",
    "__version__",
]
