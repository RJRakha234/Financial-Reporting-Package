"""The table itself: labels, exact values, and text/CSV rendering."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from fractions import Fraction

from .units import RoundingSpec, parse_value


@dataclass
class Table:
    """A rectangular block of figures with row and column labels.

    Empty cells are carried as zero but remembered (``blank``) so that a
    presentation gap stays a gap in the output instead of turning into a 0.
    """

    values: list[list[Fraction]]
    row_labels: list[str] = field(default_factory=list)
    col_labels: list[str] = field(default_factory=list)
    blank: list[list[bool]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("the table has no rows")
        width = len(self.values[0])
        if width == 0:
            raise ValueError("the table has no columns")
        if any(len(row) != width for row in self.values):
            raise ValueError("every row must have the same number of columns")
        if not self.row_labels:
            self.row_labels = [f"row {i + 1}" for i in range(self.n_rows)]
        if not self.col_labels:
            self.col_labels = [f"col {j + 1}" for j in range(self.n_cols)]
        if not self.blank:
            self.blank = [[False] * width for _ in self.values]

    @property
    def n_rows(self) -> int:
        return len(self.values)

    @property
    def n_cols(self) -> int:
        return len(self.values[0])

    def column(self, j: int) -> list[Fraction]:
        return [row[j] for row in self.values]

    @classmethod
    def from_rows(cls, rows, row_labels=None, col_labels=None) -> "Table":
        """Build from any nested sequence of numbers or figure-like strings."""
        values: list[list[Fraction]] = []
        blank: list[list[bool]] = []
        for r, row in enumerate(rows):
            parsed: list[Fraction] = []
            missing: list[bool] = []
            for c, cell in enumerate(row):
                value = parse_value(cell)
                if value is None:
                    if cell not in (None, "") and str(cell).strip():
                        raise ValueError(
                            f"cell at row {r + 1}, column {c + 1} is not a number: {cell!r}"
                        )
                    value, gap = Fraction(0), True
                else:
                    gap = False
                parsed.append(value)
                missing.append(gap)
            values.append(parsed)
            blank.append(missing)
        return cls(
            values=values,
            row_labels=list(row_labels or []),
            col_labels=list(col_labels or []),
            blank=blank,
        )

    @classmethod
    def from_csv(cls, path: str, header: bool = True, index: bool = True) -> "Table":
        with open(path, newline="", encoding="utf-8-sig") as handle:
            raw = [row for row in csv.reader(handle) if any(str(c).strip() for c in row)]
        if not raw:
            raise ValueError(f"{path} contains no data")

        col_labels: list[str] = []
        if header:
            head, raw = raw[0], raw[1:]
            col_labels = [c.strip() for c in (head[1:] if index else head)]
        row_labels: list[str] = []
        if index:
            row_labels = [row[0].strip() for row in raw]
            raw = [row[1:] for row in raw]
        width = max(len(row) for row in raw)
        raw = [row + [""] * (width - len(row)) for row in raw]
        if col_labels:
            col_labels = (col_labels + [""] * width)[:width]
        return cls.from_rows(raw, row_labels=row_labels, col_labels=col_labels)

    def render(self, cells: list[list[str]], marks: set[tuple[int, int]] = frozenset()) -> str:
        """Right-align a grid of already-formatted figures under its headings."""
        label_width = max([len(l) for l in self.row_labels] + [0])
        shown = [
            [text + ("*" if (i, j) in marks else "") for j, text in enumerate(row)]
            for i, row in enumerate(cells)
        ]
        widths = [
            max([len(self.col_labels[j])] + [len(row[j]) for row in shown])
            for j in range(self.n_cols)
        ]
        lines = [
            "  ".join([" " * label_width] + [self.col_labels[j].rjust(widths[j]) for j in range(self.n_cols)]).rstrip()
        ]
        for i, row in enumerate(shown):
            lines.append(
                "  ".join(
                    [self.row_labels[i].ljust(label_width)]
                    + [row[j].rjust(widths[j]) for j in range(self.n_cols)]
                ).rstrip()
            )
        return "\n".join(lines)

    def csv_rows(self, cells: list[list[str]]) -> list[list[str]]:
        rows = [[""] + list(self.col_labels)]
        for i, row in enumerate(cells):
            rows.append([self.row_labels[i]] + list(row))
        return rows

    def formatted(self, values: list[list[Fraction]], spec: RoundingSpec, thousands=True):
        return [
            [
                "" if self.blank[i][j] else spec.format(values[i][j], thousands=thousands)
                for j in range(self.n_cols)
            ]
            for i in range(self.n_rows)
        ]
