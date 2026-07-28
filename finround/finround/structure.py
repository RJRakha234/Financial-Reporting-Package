"""Working out which rows and columns are totals, and what they add up.

A table's rounding constraints are exactly its additive structure, so that
structure has to be recovered before anything can be rounded. Totals are
matched **backwards**, the way :mod:`fincheck` reconciles a statement: for a
row that looks like a total, walk the preceding rows most-recent-first and take
the longest trailing block that adds up to it *in every column at once*.
Agreement across all columns makes a coincidental match very unlikely.

A matched block is then rolled up into a single node, so an outer total sums
its subtotals rather than the raw lines and arbitrarily deep nesting works.
Anything that does not reconcile is left as an ordinary line and reported —
better an honest "I could not verify this total" than a fabricated constraint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

_TOTAL_RE = re.compile(
    r"(?i)\b(?:sub-?\s*totals?|totals?|aggregate|sum\s+of|grand\s+total)\b"
)


def is_total_label(label: str) -> bool:
    return bool(_TOTAL_RE.search(label or ""))


@dataclass(eq=False)
class Node:
    """A row (or column) in the additive tree; ``index`` is its position.

    The tree root is virtual — ``index is None`` — and stands for "everything",
    which lets a table without a grand total be handled by the same code.
    """

    index: int | None
    label: str = ""
    children: list["Node"] = field(default_factory=list)
    depth: int = 0

    @property
    def is_leaf(self) -> bool:
        return not self.children


def detect_groups(
    labels: list[str],
    vectors: list[list[Fraction]],
    candidates: set[int] | None = None,
    tolerance: Fraction = Fraction(0),
) -> tuple[dict[int, list[int]], list[int]]:
    """Find ``{total index: [member indices]}`` plus totals that did not foot.

    ``vectors[i]`` holds item *i*'s figures along the other axis; a candidate
    only becomes a total when a trailing block reconciles in all of them.
    """
    groups: dict[int, list[int]] = {}
    unreconciled: list[int] = []
    pending: list[int] = []

    for i, label in enumerate(labels):
        looks_total = i in candidates if candidates is not None else is_total_label(label)
        if looks_total and pending:
            members = _match_backwards(i, pending, vectors, tolerance)
            if members:
                groups[i] = members
                del pending[len(pending) - len(members) :]
                pending.append(i)
                continue
            unreconciled.append(i)
        elif looks_total:
            unreconciled.append(i)
        pending.append(i)

    return groups, unreconciled


def _match_backwards(
    total: int,
    pending: list[int],
    vectors: list[list[Fraction]],
    tolerance: Fraction,
) -> list[int]:
    stated = vectors[total]
    # Longest first: a trailing block of zeros belongs to the group it sits in.
    for size in range(len(pending), 0, -1):
        members = pending[len(pending) - size :]
        if all(
            abs(sum(vectors[m][col] for m in members) - stated[col]) <= tolerance
            for col in range(len(stated))
        ):
            return list(members)
    return []


def build_forest(count: int, groups: dict[int, list[int]], labels: list[str]) -> Node:
    """Turn ``{total: members}`` into a tree rooted at a virtual "everything"."""
    nodes = {i: Node(index=i, label=labels[i] if i < len(labels) else "") for i in range(count)}
    claimed: set[int] = set()
    for total, members in groups.items():
        if total not in nodes:
            raise ValueError(f"total index {total} out of range")
        for member in members:
            if member not in nodes:
                raise ValueError(f"member index {member} out of range")
            if member == total:
                raise ValueError(f"row {total} cannot be its own component")
            if member in claimed:
                raise ValueError(f"index {member} belongs to two different totals")
            claimed.add(member)
        nodes[total].children = [nodes[m] for m in members]

    root = Node(index=None, label="", children=[nodes[i] for i in range(count) if i not in claimed])
    _assign_depth(root, 0)
    _reject_cycles(root, count)
    return root


def _assign_depth(node: Node, depth: int) -> None:
    node.depth = depth
    for child in node.children:
        _assign_depth(child, depth + 1)


def _reject_cycles(root: Node, count: int) -> None:
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.index is not None:
            if node.index in seen:
                raise ValueError("the total structure contains a cycle")
            seen.add(node.index)
        stack.extend(node.children)
    if len(seen) != count:
        raise ValueError("the total structure does not cover every row/column")


def walk(root: Node) -> list[Node]:
    """Every node of the tree, parents before children."""
    out: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(node.children))
    return out
