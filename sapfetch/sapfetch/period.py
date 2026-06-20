"""The reporting *period* — the parameter set that drives every report run.

The portal's prompt screen (see the "GR INDAS Consolidated PL" prompts) asks for
Consol Group, Fiscal Year, From Period and To Period. A :class:`Period` captures
exactly those, validates them, and knows how to turn itself into the
``{prompt-label: value}`` mapping the portal expects.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigError


@dataclass(frozen=True)
class Period:
    """A reporting period to request from the portal.

    Attributes:
        fiscal_year: e.g. ``2025``.
        from_period: first posting period (1-16; 1 = first month of the year).
        to_period:   last posting period, inclusive. Defaults to ``from_period``.
        consol_group: consolidation group key (e.g. ``"G_GRUP"``). Optional —
            most reports default it, but it can be overridden here or per report.
    """

    fiscal_year: int
    from_period: int
    to_period: int | None = None
    consol_group: str | None = None

    def __post_init__(self) -> None:
        if self.to_period is None:
            # frozen dataclass: bypass the setattr guard for the derived default.
            object.__setattr__(self, "to_period", self.from_period)
        if self.fiscal_year < 1900 or self.fiscal_year > 9999:
            raise ConfigError(f"implausible fiscal_year: {self.fiscal_year!r}")
        for label, value in (("from_period", self.from_period),
                             ("to_period", self.to_period)):
            if not (1 <= value <= 16):
                raise ConfigError(
                    f"{label} must be between 1 and 16 (got {value!r})"
                )
        if self.to_period < self.from_period:
            raise ConfigError(
                f"to_period ({self.to_period}) is before "
                f"from_period ({self.from_period})"
            )

    @property
    def label(self) -> str:
        """A filesystem-friendly tag, e.g. ``FY2025_P01-10``."""
        return f"FY{self.fiscal_year}_P{self.from_period:02d}-{self.to_period:02d}"

    def as_prompt_values(self, labels: "PromptLabels") -> dict[str, str]:
        """Map this period onto the portal's prompt field labels.

        ``consol_group`` is only included when set, so a report-level or
        config-level default is not clobbered by an empty period value.
        """
        values: dict[str, str] = {
            labels.fiscal_year: str(self.fiscal_year),
            labels.from_period: str(self.from_period),
            labels.to_period: str(self.to_period),
        }
        if self.consol_group:
            values[labels.consol_group] = self.consol_group
        return values


# Imported lazily-ish: PromptLabels lives in config to avoid a cycle at call
# time, but the annotation above references it. Import here for runtime use.
from .config import PromptLabels  # noqa: E402  (placed last to avoid a cycle)
