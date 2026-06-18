"""Align the lines of two documents by their normalised (language-only) text and
classify every line as matched or mismatched.

Matching is a sequence alignment (``difflib.SequenceMatcher``) over the
normalised lines, so it tolerates inserted/removed/reordered lines rather than
requiring a strict 1:1 row correspondence.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .extract import Line
from .normalize import is_blank, normalize


@dataclass
class RowPair:
    """One row of the aligned comparison (for side-by-side reporting)."""

    kind: str  # "match" | "mismatch"
    a: Line | None
    b: Line | None


@dataclass
class Result:
    lines_a: list[Line]
    lines_b: list[Line]
    rows: list[RowPair] = field(default_factory=list)
    matched: int = 0
    mismatched_a: int = 0
    mismatched_b: int = 0

    @property
    def consistent(self) -> bool:
        return self.mismatched_a == 0 and self.mismatched_b == 0

    @property
    def coverage(self) -> float:
        total = self.matched + self.mismatched_a + self.mismatched_b
        return (self.matched / total) if total else 1.0


def compare_lines(
    lines_a: list[Line],
    lines_b: list[Line],
    mask_currency: bool = True,
    ignore_case: bool = True,
) -> Result:
    for ln in lines_a + lines_b:
        ln.norm = normalize(ln.text, mask_currency, ignore_case)

    # Only meaningful (non-blank) lines take part in the alignment; blank lines
    # are marked matched so they are never flagged.
    idx_a = [i for i, ln in enumerate(lines_a) if not is_blank(ln.norm)]
    idx_b = [i for i, ln in enumerate(lines_b) if not is_blank(ln.norm)]
    seq_a = [lines_a[i].norm for i in idx_a]
    seq_b = [lines_b[i].norm for i in idx_b]

    for ln in lines_a + lines_b:
        if is_blank(ln.norm):
            ln.status = "match"

    sm = SequenceMatcher(None, seq_a, seq_b, autojunk=False)
    result = Result(lines_a=lines_a, lines_b=lines_b)

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_lines = [lines_a[idx_a[k]] for k in range(i1, i2)]
        b_lines = [lines_b[idx_b[k]] for k in range(j1, j2)]
        if tag == "equal":
            for la, lb in zip(a_lines, b_lines):
                la.status = lb.status = "match"
                result.rows.append(RowPair("match", la, lb))
                result.matched += 1
        else:
            for ln in a_lines:
                ln.status = "mismatch"
                result.mismatched_a += 1
            for ln in b_lines:
                ln.status = "mismatch"
                result.mismatched_b += 1
            # Pair them up for the side-by-side view; pad the shorter side.
            for k in range(max(len(a_lines), len(b_lines))):
                la = a_lines[k] if k < len(a_lines) else None
                lb = b_lines[k] if k < len(b_lines) else None
                result.rows.append(RowPair("mismatch", la, lb))

    return result
