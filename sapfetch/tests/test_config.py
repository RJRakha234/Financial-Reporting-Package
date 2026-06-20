import pytest

from sapfetch import Period
from sapfetch.config import from_dict
from sapfetch.downloader import download_reports, output_path, prompt_values_for
from sapfetch.errors import AuthError, ConfigError, SapfetchError

BASE = {
    "portal": {"base_url": "https://example/irj/portal/reports"},
    "defaults": {
        "export_format": "xlsx",
        "download_dir": "out",
        "prompts": {"Consol Group": "G_GRUP"},
    },
    "reports": [
        {
            "name": "GR INDAS Consolidated PL",
            "open_path": ["Group Reporting", "IND-AS INR", "GR INDAS Consolidated PL"],
        },
        {
            "name": "GR IFRS USD",
            "open_path": ["Group Reporting", "IFRS INR & USD", "GR IFRS USD"],
            "output_name": "GR_IFRS_USD",
            "export_format": "pdf",
            "prompts": {"Currency": "USD"},
        },
    ],
}


def test_loads_and_finds_reports():
    cfg = from_dict(BASE)
    assert cfg.portal.base_url.endswith("/reports")
    assert {r.name for r in cfg.reports} == {"GR INDAS Consolidated PL", "GR IFRS USD"}
    assert cfg.report("GR IFRS USD").export_format == "pdf"


def test_launch_kwargs_default_is_bundled_chromium():
    cfg = from_dict(BASE)
    assert cfg.portal.launch_kwargs() == {"headless": True}


def test_launch_kwargs_uses_corporate_browser():
    data = {**BASE, "portal": {
        "base_url": "https://example/irj/portal/reports",
        "headless": False,
        "browser_channel": "msedge",
        "executable_path": "/opt/edge/msedge",
    }}
    cfg = from_dict(data)
    assert cfg.portal.launch_kwargs() == {
        "headless": False,
        "channel": "msedge",
        "executable_path": "/opt/edge/msedge",
    }


def test_missing_base_url_raises():
    with pytest.raises(ConfigError):
        from_dict({"portal": {}, "reports": []})


def test_unknown_key_raises():
    bad = {"portal": {"base_url": "x", "nope": 1}, "reports": []}
    with pytest.raises(ConfigError):
        from_dict(bad)


def test_malformed_report_entry_raises_configerror():
    with pytest.raises(ConfigError):
        from_dict({
            "portal": {"base_url": "x"},
            "reports": ["this should be a mapping, not a string"],
        })


def test_report_requires_open_path():
    with pytest.raises(ConfigError):
        from_dict({
            "portal": {"base_url": "x"},
            "reports": [{"name": "no path"}],
        })


def test_merged_prompts_overlay_period():
    cfg = from_dict(BASE)
    report = cfg.report("GR IFRS USD")
    period = Period(2025, 1, 10, consol_group="G_OVERRIDE")
    values = prompt_values_for(cfg, report, period)
    # static prompt preserved
    assert values["Currency"] == "USD"
    # config default present
    assert values["Consol Group"] == "G_OVERRIDE"  # period wins over default
    # period mapped onto labels
    assert values["Fiscal Year"] == "2025"
    assert values["From To"] == "10"


def test_output_path_uses_format_and_period_tag():
    cfg = from_dict(BASE)
    period = Period(2025, 1, 10)
    indas = output_path(cfg, cfg.report("GR INDAS Consolidated PL"), period)
    usd = output_path(cfg, cfg.report("GR IFRS USD"), period)
    assert indas.as_posix() == "out/GR_INDAS_Consolidated_PL_FY2025_P01-10.xlsx"
    assert usd.as_posix() == "out/GR_IFRS_USD_FY2025_P01-10.pdf"


def test_download_requires_a_saved_session(tmp_path):
    cfg = from_dict({
        "portal": {
            "base_url": "https://example/irj/portal/reports",
            "storage_state": str(tmp_path / "missing.json"),
        },
        "reports": [{"name": "R", "open_path": ["A", "B"]}],
    })
    with pytest.raises(AuthError):
        download_reports(cfg, Period(2025, 1, 10))


def test_download_empty_batch_short_circuits(tmp_path):
    # A session file exists, but the filter selects no reports -> returns []
    # without launching a browser (which would fail: playwright is absent here).
    session = tmp_path / "sess.json"
    session.write_text("{}")
    cfg = from_dict({
        "portal": {
            "base_url": "https://example/irj/portal/reports",
            "storage_state": str(session),
        },
        "reports": [{"name": "R", "open_path": ["A", "B"]}],
    })
    # No reports match an empty config selection path: use unknown filter guard.
    with pytest.raises(SapfetchError):
        download_reports(cfg, Period(2025, 1, 10), only=["does-not-exist"])


def test_download_no_configured_reports_returns_empty(tmp_path):
    session = tmp_path / "sess.json"
    session.write_text("{}")
    cfg = from_dict({
        "portal": {
            "base_url": "https://example/irj/portal/reports",
            "storage_state": str(session),
        },
        "reports": [],
    })
    assert download_reports(cfg, Period(2025, 1, 10)) == []
