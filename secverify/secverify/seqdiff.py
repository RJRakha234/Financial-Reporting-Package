"""Ordered number-sequence alignment between the PDF and the HTML.

The base number check asks only *"does this value exist anywhere in the PDF?"* —
a set-membership test (``key in number_counts``) with every trace of position
discarded.  That is conclusive for a distinctive figure (``1,78,650`` occurs
once in a filing, so finding it proves it) and worthless for a common one (the
digit ``3`` occurs on nearly every page, so finding it proves nothing).  Small
numbers — list enumerators, footnote markers, note references, bare counts —
therefore could not be verified at all, and were left un-asserted rather than
stamped with a green they had not earned.

This module closes that gap by checking **position instead of presence**.  Every
number in the PDF is collected in reading order and every number in the HTML in
DOM order, giving two sequences that a faithful conversion makes identical.
Aligning them with a longest-common-subsequence diff verifies each number by its
*place in the sequence* rather than by its value — so a bare ``3`` is checked as
reliably as a six-figure amount, because it is element *n* and its counterpart is
whatever sits at the aligned position on the other side.

The diff's opcodes map straight onto the error classes:

``replace``
    a value substituted — the PDF's ``3`` against the HTML's ``8``.
``delete``
    a PDF number the HTML never reproduces.
``insert``
    a HTML number with no counterpart in the PDF.

A transposition (two segments swapped) surfaces as an adjacent delete/insert
pair or a ``replace`` block, so reordering is caught as well as substitution.

Layout noise that legitimately differs between a print and a web rendering is
excluded *before* aligning, so it cannot masquerade as a substitution: page
numbers, per-page header and footer reprints, index/TOC page references, and the
digits inside identifiers (DIN, UDIN, membership and registration numbers).

Findings are tiered rather than suppressed.  A tight opcode covering a handful of
numbers is reported individually with both sides quoted; a large block (a whole
section the HTML omits, already covered by the PDF-coverage check) is aggregated
into one summary finding.  Nothing is dropped in silence — see the module note in
``grid.py`` for why a check that quietly declines is the one unacceptable output.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .numbers import iter_tokens
from .textnorm import canonical, running_furniture

#: a line that is nothing but a page number ("12", "Page 3 of 40", "| 7 |")
_PAGE_ONLY_RE = re.compile(
    r"^\s*[|\-–—]?\s*(?:page\s*)?\d{1,3}\s*(?:of\s+\d{1,3})?\s*[|\-–—]?\s*$", re.I
)
#: how many lines at the top/bottom of a page count as header/footer territory
_EDGE_LINES = 3
#: opcodes spanning more than this many numbers are aggregated into one summary
#: finding instead of reported one by one
_TIGHT_SPAN = 4
#: at most this many individual sequence findings; the remainder is summarised so
#: a badly desynchronised document cannot bury the report
_MAX_INDIVIDUAL = 40


def _numbers_in(text: str) -> list[tuple[str, str]]:
    """``(key, token)`` for every number in *text*, identifiers excluded.

    The same identifier filters the annotator applies: a leading-zero run is a
    DIN/registration number, and a digit run glued to a preceding letter is the
    numeric tail of a code ("A21918", "…BMOCJH8380").  Neither is an amount.
    """
    out: list[tuple[str, str]] = []
    for start, _end, token, key in iter_tokens(text):
        t = token.strip()
        if re.match(r"^\(?0\d", t):
            continue
        if start > 0 and text[start - 1].isalpha():
            continue
        out.append((key, t))
    return out


def pdf_number_sequence(pages_raw: list[str]) -> list[tuple[str, str]]:
    """``(key, context)`` for every PDF number in reading order.

    Page numbers and reprinted headers/footers are dropped.  A header line that
    the print layout repeats on every page exists **once** in the HTML, so
    keeping only its first occurrence is what makes the two sequences
    comparable — dropping every occurrence would instead lose real content.
    """
    seq: list[tuple[str, str]] = []
    # A running header/footer carries a page number that changes every page and
    # appears in the HTML nowhere; it is dropped outright.  A reprinted table
    # column header repeats identically and DOES appear in the HTML once, so its
    # first occurrence is kept and later ones dropped.
    furniture = running_furniture(pages_raw)
    seen_edge: set[str] = set()
    for raw in pages_raw:
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        n = len(lines)
        for pos, line in enumerate(lines):
            if _PAGE_ONLY_RE.match(line):
                continue
            at_edge = pos < _EDGE_LINES or pos >= n - _EDGE_LINES
            if at_edge and canonical(line, letters_only=True) in furniture:
                continue  # running page header/footer — print furniture
            c = canonical(line)
            if at_edge and c:
                if c in seen_edge:
                    continue  # a header/footer reprint — counted once already
                seen_edge.add(c)
            ctx = " ".join(line.split())[:120]
            for key, token in _numbers_in(line):
                seq.append((key, ctx))
    return seq


def html_number_sequence(soup) -> list[tuple[str, str]]:
    """``(key, context)`` for every HTML number in DOM (reading) order."""
    seq: list[tuple[str, str]] = []
    for node in soup.find_all(string=True):
        parent = getattr(node, "parent", None)
        if parent is not None and parent.name in ("script", "style"):
            continue
        text = str(node)
        if not text.strip() or not re.search(r"\d", text):
            continue
        ctx = " ".join(text.split())[:120]
        for key, _token in _numbers_in(text):
            seq.append((key, ctx))
    return seq


def _fmt(items: list[tuple[str, str]], lo: int, hi: int, limit: int = 6) -> str:
    vals = [k for k, _c in items[lo:hi]]
    shown = ", ".join(vals[:limit])
    return shown + (f" … (+{len(vals) - limit} more)" if len(vals) > limit else "")


def sequence_findings(pages_raw, soup):
    """Yield ``(kind, severity, excerpt, remark)`` for number-sequence breaks."""
    pdf_seq = pdf_number_sequence(pages_raw)
    html_seq = html_number_sequence(soup)
    if len(pdf_seq) < 20 or len(html_seq) < 20:
        return  # too little to align meaningfully

    pkeys = [k for k, _c in pdf_seq]
    hkeys = [k for k, _c in html_seq]
    # autojunk MUST be off: it treats elements appearing in >1% of a long
    # sequence as noise, which is exactly the common small numbers this check
    # exists to verify.
    sm = SequenceMatcher(None, pkeys, hkeys, autojunk=False)

    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]

    # Reflow of a REPEATED CAPTION DIGIT.  A print layout sets a two-line column
    # caption as "3 months ended / 3 months ended" with the dates beneath, while
    # the HTML pairs each caption with its own date — so one "3" shifts a few
    # positions.  This surfaces as a delete of "3" and an insert of "3" nearby.
    #
    # The suppression is deliberately narrow: a SINGLE bare 1-3 digit value that
    # already occurs more than once in the local window of BOTH sequences.  Under
    # those conditions the move is a genuine no-op — the value is changing places
    # only with its own identical twin, so no label ends up against a different
    # figure and there is no error that could hide here.
    #
    # It must stay narrow.  A wider rule ("same values deleted and inserted
    # nearby") also matches a real swap of two line items' figures — Hi-Tech and
    # Retail exchanging 3,710/3,558 for 6,172/5,958 — which must always be
    # reported, since every value is present and only the pairing is wrong.
    _WIN = 10
    reflow: set[int] = set()
    for a, (tag_a, i1, i2, _j1, _j2) in enumerate(ops):
        if tag_a != "delete" or i2 - i1 != 1 or a in reflow:
            continue
        val = pkeys[i1]
        if not re.fullmatch(r"\d{1,3}", val):
            continue  # only a bare caption digit, never a real amount
        for b in range(max(0, a - 2), min(len(ops), a + 3)):
            if b == a or b in reflow or ops[b][0] != "insert":
                continue
            _t, _bi1, _bi2, bj1, bj2 = ops[b]
            if bj2 - bj1 != 1 or hkeys[bj1] != val:
                continue
            p_local = pkeys[max(0, i1 - _WIN):i1 + _WIN]
            h_local = hkeys[max(0, bj1 - _WIN):bj1 + _WIN]
            if p_local.count(val) > 1 and h_local.count(val) > 1:
                reflow.update((a, b))
                break

    individual = 0
    bulk = {"replace": 0, "delete": 0, "insert": 0}
    reflowed = 0
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if idx in reflow:
            reflowed += max(i2 - i1, j2 - j1)
            continue
        span = max(i2 - i1, j2 - j1)
        if span > _TIGHT_SPAN or individual >= _MAX_INDIVIDUAL:
            bulk[tag] += span
            continue
        individual += 1
        if tag == "replace":
            pdf_ctx = pdf_seq[i1][1] if i1 < len(pdf_seq) else ""
            one = (i2 - i1) == 1 and (j2 - j1) == 1
            yield (
                "seq-value",
                "error" if one else "review",
                f"PDF {_fmt(pdf_seq, i1, i2)} → HTML {_fmt(html_seq, j1, j2)}",
                f"Number out of sequence — at this point the PDF reads "
                f"{_fmt(pdf_seq, i1, i2)} but the HTML reads "
                f"{_fmt(html_seq, j1, j2)}. Every number before and after "
                f"aligns, so this is a substitution rather than a shift. PDF "
                f"context: “{pdf_ctx}”. This check verifies a number by its "
                "position in the document's number sequence, so it covers small "
                "values (list markers, note references, counts) that a "
                "presence-only check cannot confirm.",
            )
        elif tag == "delete":
            yield (
                "seq-missing",
                "review",
                f"PDF {_fmt(pdf_seq, i1, i2)} has no HTML counterpart here",
                f"Number not reproduced — the PDF has {_fmt(pdf_seq, i1, i2)} at "
                f"this point in its number sequence with nothing corresponding in "
                f"the HTML. PDF context: “{pdf_seq[i1][1]}”. Either the value was "
                "dropped, or it moved elsewhere in the document.",
            )
        else:  # insert
            yield (
                "seq-extra",
                "review",
                f"HTML {_fmt(html_seq, j1, j2)} has no PDF counterpart here",
                f"Number with no source — the HTML has {_fmt(html_seq, j1, j2)} at "
                f"this point in its number sequence with nothing corresponding in "
                f"the PDF. HTML context: “{html_seq[j1][1]}”. Either the value was "
                "added, or it moved from elsewhere in the document.",
            )

    if any(bulk.values()):
        parts = [f"{v} {k}d" for k, v in bulk.items() if v]
        yield (
            "seq-bulk",
            "review",
            f"{sum(bulk.values())} further numbers differ in sequence",
            "Number sequence — beyond the individually listed items, "
            f"{sum(bulk.values())} numbers fall in large runs that do not align "
            f"({', '.join(parts)}). Large runs usually mean a whole section is "
            "ordered differently, omitted, or added, rather than individual "
            "values being wrong — the content-ordering and PDF-coverage checks "
            "name those sections. Reported here so the count is never invisible.",
        )
