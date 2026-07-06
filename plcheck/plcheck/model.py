"""Lightweight data structures shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Entity:
    """One reporting entity = one column within every block."""

    code: str          # "BALSCH"
    name: str          # "Base life science AG"
    currency: str      # "CHF"


@dataclass
class Block:
    """A four-column band of the report, e.g. "GC - Total"."""

    label: str                       # "LC - Balance"
    # sub-header -> source column letter in the *input report*
    columns: dict[str, str] = field(default_factory=dict)

    def entity_col(self, code: str) -> str | None:
        return self.columns.get(code)

    def overall_col(self, overall_label: str) -> str | None:
        return self.columns.get(overall_label)


@dataclass
class ReportRow:
    """One row of the report's data area."""

    category: str                     # "Revenue" (blank on subtotal rows)
    account: object                   # GL number, or None on subtotal rows
    description: str
    # (block label, sub-header) -> numeric value
    values: dict[tuple[str, str], float] = field(default_factory=dict)

    @property
    def is_subtotal(self) -> bool:
        return self.account in (None, "")


@dataclass
class ReportTable:
    """The whole parsed IFRS P&L report."""

    entities: list[Entity]
    blocks: list[Block]
    rows: list[ReportRow]
    # cells in the report that hold an Excel error (e.g. "#REF!"), described
    # for the warning surfaced to the user.
    errors: list[str] = field(default_factory=list)

    def block(self, label: str) -> Block | None:
        for b in self.blocks:
            if b.label == label:
                return b
        return None


@dataclass
class SheetData:
    """A verbatim copy of an input sheet (values and/or formulas)."""

    title: str
    cells: dict[str, object]   # "A1" -> value
    max_row: int
    max_col: int
    # coordinates whose value is *text* that merely looks like a formula
    # (a note typed with a leading "="); they must be written back as text,
    # or Excel treats them as malformed formulas and offers to "repair".
    text_coords: set = field(default_factory=set)
