"""Recover a table's columns from where its figures sit on the page.

A PDF states no table structure, so a row arrives as a label and a list of
figures. Reading those lists by position is wrong the moment a row skips a
column: in a segment note, "Unallocable expenses" carries a single amount that
belongs under *Total*, and read by position it lands under the first segment,
beside amounts it has nothing to do with. Every figure below it is then a column
out of step, which is precisely the mistake a reviewer is using this tool to
catch.

Financial tables are right-aligned, so a figure's right edge is the same to
within a point or two all the way down its column. Clustering those right edges
recovers the columns, and the same edges say which column a heading belongs
over — headings are set to the same alignment. That gives a grid a row can be
placed into, and a name for each column to place it under.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Right edges within this many points are the same column. Figures of different
# widths in one column agree to about a point; the next column is tens away.
_COLUMN_TOLERANCE = 4.0
# A column needs to appear in this fraction of a table's rows before it is a
# column rather than a stray number. Kept low: in a segment note most columns
# are filled on only the handful of rows that are segmented, the rest of the
# statement running through Total alone. The real guard is that a grid too
# narrow to hold the widest row is rejected outright.
_COLUMN_SHARE = 0.1
_MIN_ROWS = 1
# Footnote references in a heading name a note, not the column.
_MARKER = re.compile(r"\(\s*\d\s*\)")
# What the label column of a table is called: "Particulars", "Component",
# "Assets:". Past this many words the lines above the table are the statement's
# title running across it, not headings naming its columns.
_LABEL_WORDS = 6
# "Total equity attributable to equity holders of the Company" is as long as a
# column heading gets. Past this the band is a paragraph running over the table.
_HEADING_WORDS = 12


@dataclass
class Grid:
    """The columns of one table, left to right."""

    edges: list[float] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    label: str = ""

    def __len__(self) -> int:
        return len(self.edges)

    @property
    def is_named(self) -> bool:
        """Did the lines above the table really name its columns?

        A heading band names most of what it sits over. One or two columns
        picking up a word while the rest stay blank is the title of the section,
        or a sentence that happened to end near a figure — not headings, and
        treating it as headings would take that line out of the prose where it
        belongs.
        """
        if len(self.label.split()) > _LABEL_WORDS:
            return False
        if any(len(h.split()) > _HEADING_WORDS for h in self.headings):
            return False
        named = sum(1 for h in self.headings if re.search(r"[A-Za-z]{2}", h))
        return named >= max(2, (len(self.edges) + 1) // 2)

    def column_of(self, x1: float) -> int:
        """Which column does a figure ending at ``x1`` belong to?"""
        best, distance = 0, None
        for index, edge in enumerate(self.edges):
            gap = abs(edge - x1)
            if distance is None or gap < distance:
                best, distance = index, gap
        return best

    def place(self, row) -> list:
        """The row's figures, one slot per column, ``None`` where it has none.

        Two figures landing in one column means the row was not read the way the
        grid describes, so the row is returned by position instead: a wrong
        placement is worse than an unhelpful one.
        """
        slots: list = [None] * len(self.edges)
        if not self.edges:
            return list(row.values)
        figures = getattr(row, "figures", None)
        if not figures:
            return slots
        for figure in figures:
            index = self.column_of(figure.x1)
            if slots[index] is not None:
                return None
            slots[index] = figure.value
        return slots


def _cluster(edges: list[float]) -> list[list[float]]:
    groups: list[list[float]] = []
    for edge in sorted(edges):
        if groups and edge - groups[-1][-1] <= _COLUMN_TOLERANCE:
            groups[-1].append(edge)
        else:
            groups.append([edge])
    return groups


def build_grid(rows) -> Grid:
    """Work out the columns of the table these rows make up."""
    figure_rows = [r for r in rows if getattr(r, "figures", None)]
    if not figure_rows:
        return Grid()

    groups = _cluster([f.x1 for r in figure_rows for f in r.figures])
    floor = max(_MIN_ROWS, round(len(figure_rows) * _COLUMN_SHARE))
    kept = [g for g in groups if len(g) >= floor] or groups
    edges = [round(sum(g) / len(g), 2) for g in kept]

    # A grid that cannot hold the widest row is not describing this table.
    widest = max(len(r.figures) for r in figure_rows)
    if len(edges) < widest:
        return Grid()
    return Grid(edges=edges)


def name_columns(grid: Grid, band) -> Grid:
    """Read the headings above a table onto its columns.

    Words are assigned by their centre to the column whose territory they fall
    in, where territory runs to the midpoint between neighbouring right edges.
    Words to the left of the first column head the label column instead.
    """
    if not grid.edges:
        return grid

    # The first column reaches back by the width of the widest column, which is
    # where the label column has to end; anything further left names the labels.
    widths = [b - a for a, b in zip(grid.edges, grid.edges[1:])]
    left = grid.edges[0] - (max(widths) if widths else _COLUMN_TOLERANCE * 8)

    parts: list[list[str]] = [[] for _ in grid.edges]
    label: list[str] = []
    for row in band:
        for _, x1, text in getattr(row, "words", ()):
            word = _MARKER.sub("", text).strip()
            if not word:
                continue
            if x1 < left:
                label.append(word)
                continue
            # A heading is set to its column's alignment, so it ends at or
            # before that column's edge and after the one before it. A word of a
            # heading that wrapped sits inside the column, which the same test
            # catches: it still ends before this edge and after the last.
            index = 0
            while (
                index < len(grid.edges) - 1
                and x1 > grid.edges[index] + _COLUMN_TOLERANCE
            ):
                index += 1
            parts[index].append(word)

    grid.headings = [" ".join(p).strip() for p in parts]
    grid.label = " ".join(label).strip()
    return grid


def _key(heading: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", heading.lower()))


@dataclass
class Match:
    """How one table's columns line up with the other's.

    ``order`` holds, for each of the benchmark's columns, the compared
    document's column that carries the same heading, or ``None`` where it has
    none. ``reordered`` says the same columns are present but not in the same
    places — a segment table whose columns have been shuffled states different
    amounts against the same headings while the figures read identically down
    the page, which comparing by position cannot see.
    """

    order: list = field(default_factory=list)
    reordered: bool = False
    named: bool = False


def match_columns(grid_a: Grid, grid_b: Grid) -> Match:
    """Line up two tables' columns by heading, falling back to position."""
    positional = Match(order=list(range(len(grid_b.edges))))
    if not grid_a.edges or not grid_b.edges:
        return positional

    if not grid_a.is_named or not grid_b.is_named:
        return positional
    keys_a = [_key(h) for h in grid_a.headings]
    keys_b = [_key(h) for h in grid_b.headings]
    if not all(keys_a) or not all(keys_b) or len(set(keys_a)) != len(keys_a):
        return positional
    if sorted(keys_a) != sorted(keys_b):
        return positional

    where = {key: index for index, key in enumerate(keys_b)}
    order = [where[key] for key in keys_a]
    return Match(order=order, reordered=order != sorted(order), named=True)


def grid_for(block) -> Grid:
    """The grid of a reconstructed table block, headings included."""
    rows = getattr(block, "rows", None)
    if not rows:
        return Grid()
    return name_columns(build_grid(rows), getattr(block, "headers", ()) or ())
