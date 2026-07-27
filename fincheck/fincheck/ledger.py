"""Independent figure reconciliation — the check that does not trust the alignment.

The side-by-side comparison pairs content by similarity and then compares the
figures inside paired rows. That is how a reviewer works, and it inherits a
reviewer's weakness: if two rows are paired wrongly, or not paired at all, the
figures they carry are measured against the wrong counterpart, or never
measured. On a wide statement split differently by the two documents, that can
turn agreeing figures into a page of false alarms — or, far worse, hide a
figure that really did change.

This module answers a narrower question that no alignment can distort:

    taken as a whole, does every figure printed in one document also appear in
    the other, the same number of times?

It reads every numeric token on every page of both files and reconciles the two
multisets. It ignores structure entirely, so it cannot say a figure sits in the
wrong row — only whether it is present, and how often. That is precisely the
assurance a reconciliation needs underneath a similarity judgement: the
alignment says *where* things differ, and this says *whether* anything was lost
or invented, with no inference in between.

Rounding is never applied. Two figures reconcile only when their parsed values
are equal, so ``1,234.50`` and ``1,234.5`` reconcile and ``1,234.50`` and
``1,234.51`` do not.
"""

from collections import defaultdict
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from .numbers import format_number, is_numberish, parse_number

# A figure this small and this round is nearly always structure — a page folio,
# a note number, a column of years — not an amount. Counted and reported
# separately rather than dropped, because "ignored it" is not an answer a
# reconciliation may give.
_STRUCTURAL_MAX = 3000.0


@dataclass
class Occurrence:
    """One printed figure, and the line it was printed on."""

    value: float
    page: int
    context: str


@dataclass
class Surplus:
    """A figure one document prints more often than the other."""

    value: float
    surplus: int
    here: int
    there: int
    occurrences: list

    @property
    def absent(self) -> bool:
        """Printed here and nowhere at all in the other document."""
        return self.there == 0

    @property
    def note(self) -> str:
        if self.absent:
            return "not printed in the other document"
        return f"printed {self.here}× here, {self.there}× there"


@dataclass
class Ledger:
    """Every figure in both documents, reconciled as multisets."""

    a: dict = field(default_factory=dict)  # value -> [Occurrence]
    b: dict = field(default_factory=dict)

    @property
    def total_a(self) -> int:
        return sum(len(v) for v in self.a.values())

    @property
    def total_b(self) -> int:
        return sum(len(v) for v in self.b.values())

    def _unmatched(self, side: str) -> list["Surplus"]:
        """Figures one side prints more often than the other.

        A figure printed three times here and twice there is a surplus of one,
        not a missing figure, and the counts travel with it so the report can
        say which it is.
        """
        mine, theirs = (self.a, self.b) if side == "a" else (self.b, self.a)
        out = []
        for value, occurrences in mine.items():
            other = len(theirs.get(value, ()))
            surplus = len(occurrences) - other
            if surplus > 0:
                out.append(
                    Surplus(
                        value=value,
                        surplus=surplus,
                        here=len(occurrences),
                        there=other,
                        occurrences=occurrences[-surplus:],
                    )
                )
        out.sort(key=lambda s: (-abs(s.value), s.value))
        return out

    @property
    def only_in_a(self) -> list["Surplus"]:
        """Figures the benchmark prints more often than the compared document."""
        return self._unmatched("a")

    @property
    def only_in_b(self) -> list["Surplus"]:
        return self._unmatched("b")

    @property
    def absent_amounts(self) -> list["Surplus"]:
        """Amounts printed in the benchmark and nowhere in the other document.

        The strongest statement the ledger makes, and the one an auditor reads
        first: not "printed a different number of times", but *absent*.
        """
        return [x for x in self._split(self.only_in_a)[0] if x.absent]

    @staticmethod
    def _split(items: list) -> tuple[list, list]:
        """Separate amounts from figures that look structural."""
        amounts = [x for x in items if abs(x.value) > _STRUCTURAL_MAX]
        structural = [x for x in items if abs(x.value) <= _STRUCTURAL_MAX]
        return amounts, structural

    @property
    def unreconciled(self) -> int:
        """Distinct amounts (not structural values) that do not reconcile."""
        return len(self._split(self.only_in_a)[0]) + len(
            self._split(self.only_in_b)[0]
        )

    @property
    def reconciled(self) -> bool:
        """Every figure in each document is matched by one in the other."""
        return not self.only_in_a and not self.only_in_b

    @property
    def coverage(self) -> float:
        """Share of the benchmark's figures found in the compared document."""
        if not self.total_a:
            return 100.0
        missing = sum(x.surplus for x in self.only_in_a)
        return 100.0 * (self.total_a - missing) / self.total_a


def _figures_on_page(page) -> list[Occurrence]:
    """Every numeric token drawn on one page, with the line it sits on."""
    words = page.get_text("words")
    lines: dict = defaultdict(list)
    for word in words:
        # Bucket by rounded baseline: the same line, whatever the x order.
        lines[round(word[3], 1)].append(word)

    found: list[Occurrence] = []
    for baseline in sorted(lines):
        bucket = sorted(lines[baseline], key=lambda w: w[0])
        text = " ".join(w[4] for w in bucket).strip()
        for word in bucket:
            token = word[4].strip()
            if not is_numberish(token):
                continue
            value = parse_number(token)
            if value is None:
                continue
            found.append(
                Occurrence(value=value, page=page.number + 1, context=text[:120])
            )
    return found


def read_figures(pdf_path: str) -> dict:
    """``{value: [Occurrence]}`` for every figure printed in the document."""
    doc = fitz.open(pdf_path)
    try:
        out: dict = defaultdict(list)
        for page in doc:
            for occurrence in _figures_on_page(page):
                out[occurrence.value].append(occurrence)
        return dict(out)
    finally:
        doc.close()


def reconcile(pdf_a: str, pdf_b: str) -> Ledger:
    """Reconcile every figure in two documents, ignoring their structure."""
    return Ledger(a=read_figures(pdf_a), b=read_figures(pdf_b))


def describe(ledger: Ledger, label_a: str = "benchmark", label_b: str = "compared") -> str:
    """A plain-text statement of the reconciliation, for the console."""
    lines = [
        f"Figure ledger: {ledger.total_a:,} figures in the {label_a}, "
        f"{ledger.total_b:,} in the {label_b}."
    ]
    if ledger.reconciled:
        lines.append(
            "  Every figure in each document appears in the other, the same "
            "number of times."
        )
        return "\n".join(lines)

    for side, items, where in (
        ("a", ledger.only_in_a, f"the {label_a} only"),
        ("b", ledger.only_in_b, f"the {label_b} only"),
    ):
        amounts, structural = Ledger._split(items)
        if not items:
            continue
        lines.append(
            f"  {sum(x.surplus for x in items):,} printed figure(s) in {where}: "
            f"{len(amounts)} distinct amount(s) and {len(structural)} distinct "
            "small value(s) that look structural (folios, note numbers, years)."
        )
        for item in amounts[:12]:
            first = item.occurrences[0]
            lines.append(
                f"    {format_number(item.value):>16}  p{first.page}  "
                f"({item.note})  {first.context[:58]}"
            )
        if len(amounts) > 12:
            lines.append(f"    … and {len(amounts) - 12} more amount(s)")
    return "\n".join(lines)
