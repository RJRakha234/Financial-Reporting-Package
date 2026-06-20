"""Configuration model and YAML loader.

Everything that is environment- or report-specific lives in a YAML file so the
code never hard-codes a URL, a report path, or a brittle CSS selector. See
``config.example.yaml`` for an annotated template.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .errors import ConfigError


@dataclass
class PromptLabels:
    """The visible labels of the period prompts, exactly as the portal shows
    them. Defaults match the "GR INDAS Consolidated PL" prompt screen."""

    consol_group: str = "Consol Group"
    fiscal_year: str = "Fiscal Year"
    from_period: str = "From Period"
    to_period: str = "From To"


@dataclass
class Selectors:
    """Tunable selectors for the WebI / Enterprise-Portal UI.

    These are the one part of the tool that genuinely depends on the live DOM,
    so they are data, not code — calibrate them once (``sapfetch login`` opens an
    inspectable browser) and they stay in your YAML. Each is a Playwright
    selector string; ``{label}`` / ``{value}`` are substituted where noted.
    """

    # The container that holds the prompt rows (used to scope the search).
    prompt_panel: str = "[role=dialog], .promptDialog, .sapMDialog"
    # A single prompt row located by its visible label. {label} is substituted.
    prompt_row_by_label: str = "*:has-text('{label}')"
    # The editable input within (or following) a prompt row.
    prompt_input: str = "input:visible, textarea:visible"
    # Button that submits the prompts / runs the report.
    run_button: str = "button:has-text('OK'), button:has-text('Run'), "\
        "button:has-text('Apply')"
    # Opens the export menu/dialog.
    export_button: str = "button[title*='Export' i], button:has-text('Export')"
    # Chooses a format inside the export dialog. {format} is substituted.
    export_format_option: str = "*:has-text('{format}')"
    # Confirms the export dialog and triggers the actual download.
    export_confirm: str = "button:has-text('Export'), button:has-text('OK'), "\
        "button:has-text('Download')"
    # A node clicked while walking ``open_path``. {step} is substituted.
    nav_item: str = "a:has-text('{step}'), *[role=treeitem]:has-text('{step}'), "\
        "*:has-text('{step}')"


@dataclass
class PortalConfig:
    base_url: str
    storage_state: str = ".sap_session.json"
    # If the report renders inside an iframe (SAP WebI usually does), a substring
    # of that frame's URL or name. ``None`` = use the main frame / auto-detect.
    report_frame: str | None = None
    headless: bool = True
    nav_timeout_ms: int = 60_000
    action_timeout_ms: int = 30_000
    # Air-gapped / corporate browser selection. By default Playwright launches
    # its own bundled Chromium (which must be staged offline). To drive the
    # browser already installed on the machine instead, set one of:
    #   browser_channel: "msedge" | "chrome" | "chrome-beta" | ...
    #   executable_path: full path to a chromium-family browser binary
    browser_channel: str | None = None
    executable_path: str | None = None

    def launch_kwargs(self) -> dict:
        """Assemble the kwargs for ``chromium.launch`` from this config."""
        kwargs: dict = {"headless": self.headless}
        if self.browser_channel:
            kwargs["channel"] = self.browser_channel
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        return kwargs


@dataclass
class Defaults:
    prompts: dict[str, str] = field(default_factory=dict)
    export_format: str = "xlsx"
    download_dir: str = "downloads"


@dataclass
class ReportSpec:
    """One downloadable report."""

    name: str
    # Click path through the portal to reach the report, e.g.
    # ["Group Reporting", "IND-AS INR", "GR INDAS Consolidated PL"].
    open_path: list[str] = field(default_factory=list)
    # Per-report prompt overrides (e.g. a currency or a fixed consol group).
    prompts: dict[str, str] = field(default_factory=dict)
    export_format: str | None = None  # falls back to Defaults.export_format
    output_name: str | None = None    # base filename; period tag is appended

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigError("each report needs a 'name'")
        if not self.open_path:
            raise ConfigError(f"report {self.name!r} needs an 'open_path'")


@dataclass
class Config:
    portal: PortalConfig
    defaults: Defaults = field(default_factory=Defaults)
    reports: list[ReportSpec] = field(default_factory=list)
    prompt_labels: PromptLabels = field(default_factory=PromptLabels)
    selectors: Selectors = field(default_factory=Selectors)

    def report(self, name: str) -> ReportSpec:
        for r in self.reports:
            if r.name == name:
                return r
        raise ConfigError(f"no report named {name!r} in config")

    def merged_prompts(self, report: ReportSpec) -> dict[str, str]:
        """Config defaults overlaid with the report's own overrides."""
        merged = dict(self.defaults.prompts)
        merged.update(report.prompts)
        return merged


def _only_known(cls: type, data: dict[str, Any], where: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"unknown key(s) in {where}: {', '.join(sorted(unknown))}"
        )
    return data


def from_dict(data: dict[str, Any]) -> Config:
    """Build (and validate) a :class:`Config` from a plain dict."""
    if "portal" not in data or "base_url" not in (data.get("portal") or {}):
        raise ConfigError("config must define portal.base_url")

    portal = PortalConfig(**_only_known(PortalConfig, data["portal"], "portal"))
    defaults = Defaults(
        **_only_known(Defaults, data.get("defaults", {}), "defaults")
    )
    prompt_labels = PromptLabels(
        **_only_known(PromptLabels, data.get("prompt_labels", {}), "prompt_labels")
    )
    selectors = Selectors(
        **_only_known(Selectors, data.get("selectors", {}), "selectors")
    )
    reports = [
        ReportSpec(**_only_known(ReportSpec, r, f"reports[{i}]"))
        for i, r in enumerate(data.get("reports", []))
    ]
    return Config(
        portal=portal,
        defaults=defaults,
        reports=reports,
        prompt_labels=prompt_labels,
        selectors=selectors,
    )


def load(path: str | Path) -> Config:
    """Load a YAML config file into a :class:`Config`."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError(
            "PyYAML is required to read config files: pip install pyyaml"
        ) from exc
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{p} must contain a YAML mapping at the top level")
    return from_dict(data)
