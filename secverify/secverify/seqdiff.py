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

from .coverage import parse_index_line
from .numbers import iter_tokens, token_attrs
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


def _numbers_in(text: str) -> list[tuple[str, str, bool, str]]:
    """``(key, token, is_percent, currency)`` per number, identifiers excluded.

    The same identifier filters the annotator applies: a leading-zero run is a
    DIN/registration number, and a digit run glued to a preceding letter is the
    numeric tail of a code ("A21918", "…BMOCJH8380").  Neither is an amount.

    The ``%``/currency attributes ride along because canonicalisation strips
    them: once the sequence diff has PAIRED a PDF token with its HTML
    counterpart, comparing those attributes on the pair is the one place a
    dropped "%" or a swapped symbol is visible without guessing.
    """
    out: list[tuple[str, str, bool, str]] = []
    for start, _end, token, key in iter_tokens(text):
        t = token.strip()
        if re.match(r"^\(?0\d", t):
            continue
        if start > 0 and text[start - 1].isalpha():
            continue
        _sign, currency, is_pct = token_attrs(t)
        out.append((key, t, is_pct, currency))
    return out


def pdf_number_sequence(pages_raw: list[str]) -> list[tuple[str, str, bool, str]]:
    """``(key, context)`` for every PDF number in reading order.

    Page numbers and reprinted headers/footers are dropped.  A header line that
    the print layout repeats on every page exists **once** in the HTML, so
    keeping only its first occurrence is what makes the two sequences
    comparable — dropping every occurrence would instead lose real content.
    """
    seq: list[tuple[str, str, bool, str]] = []
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
            # A print index/TOC entry ("2.7 Property, plant and equipment ……… 19")
            # ends in a page number that an unpaginated HTML has no way to carry.
            # Only the label's own numbers (the "2.7") take part — the same rule
            # the PDF-coverage check applies to index lines.
            entry = parse_index_line(line)
            src = entry[0] if entry else line
            ctx = " ".join(line.split())[:120]
            for key, _tok, is_pct, cur in _numbers_in(src):
                seq.append((key, ctx, is_pct, cur))
    return seq


def html_number_sequence(soup) -> list[tuple[str, str, bool, bool, str]]:
    """``(key, context, in_table)`` for every HTML number in DOM order.

    ``is_data_cell`` marks a figure sitting alone in a grid cell — a short,
    essentially letter-free cell such as ``17,447`` or ``20.8%``.  It decides how
    confidently a sequence break can be reported: a data grid's cell order is
    preserved by both renderings, whereas prose is reflowed and its PDF
    extraction routinely runs words together ("OnApril9,2024,IASBha"), so the
    order of numbers inside a sentence is not a reliable signal on its own.

    Table MEMBERSHIP is deliberately not the test.  SEC exhibit HTML nests
    everything — headings, bullets, whole paragraphs — inside layout tables, so
    keying on ``<table>`` classified running prose as grid data and silently
    disabled the prose-only checks on every real filing.  What matters is
    whether the text node is a bare figure or a sentence, which is what this
    measures.
    """
    seq: list[tuple[str, str, bool, bool, str]] = []
    for node in soup.find_all(string=True):
        parent = getattr(node, "parent", None)
        if parent is not None and parent.name in ("script", "style"):
            continue
        text = str(node)
        if not text.strip() or not re.search(r"\d", text):
            continue
        stripped = text.strip()
        in_table = parent is not None and parent.find_parent("table") is not None
        # a data cell: inside a grid AND holding essentially no words
        is_data_cell = in_table and len(re.findall(r"[A-Za-z]", stripped)) <= 3
        ctx = " ".join(text.split())[:120]
        for key, _tok, is_pct, cur in _numbers_in(text):
            seq.append((key, ctx, is_data_cell, is_pct, cur))
    return seq


def _fmt(items, lo: int, hi: int, limit: int = 6) -> str:
    vals = [it[0] for it in items[lo:hi]]
    shown = ", ".join(vals[:limit])
    return shown + (f" … (+{len(vals) - limit} more)" if len(vals) > limit else "")


def sequence_findings(pages_raw, soup):
    """Yield ``(kind, severity, excerpt, remark)`` for number-sequence breaks."""
    pdf_seq = pdf_number_sequence(pages_raw)
    html_seq = html_number_sequence(soup)
    if len(pdf_seq) < 20 or len(html_seq) < 20:
        return  # too little to align meaningfully

    pkeys = [it[0] for it in pdf_seq]
    hkeys = [it[0] for it in html_seq]
    # autojunk MUST be off: it treats elements appearing in >1% of a long
    # sequence as noise, which is exactly the common small numbers this check
    # exists to verify.
    sm = SequenceMatcher(None, pkeys, hkeys, autojunk=False)

    # Symbols on ALIGNED occurrences.  Canonicalisation strips "%", "₹" and "$"
    # so magnitudes match across formatting, which means a symbol change alone
    # leaves every digit and every count intact.  The sequence diff has already
    # paired each PDF token with its HTML counterpart, so comparing attributes on
    # an ``equal`` pair isolates a genuine symbol change from a merely missing
    # occurrence — the failure mode that made a document-wide COUNT census
    # unusable (a figure rendered as an image reads as a dropped "%").
    #
    # Restricted to PROSE — meaning a sentence, NOT merely "outside a <table>".
    # A grid cell conventionally leaves the unit to its column header, so an
    # aligned pair legitimately differs there and the row-value check owns it.
    # But SEC exhibits nest prose inside layout tables, so testing table
    # membership disabled this on every real filing; the test is whether the
    # text node is a bare figure or running text.
    sym_hits = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for off in range(i2 - i1):
            p = pdf_seq[i1 + off]
            h = html_seq[j1 + off]
            if h[2]:
                continue  # a bare grid figure — owned by the row-value check
            if p[2] != h[3]:
                sym_hits += 1
                gone = p[2] and not h[3]
                yield (
                    "percent",
                    "error",
                    f"{p[0]}: PDF {'has' if p[2] else 'has no'} % / HTML "
                    f"{'has' if h[3] else 'has no'} %",
                    f"Percent sign — at this point the PDF reads “{p[0]}%” but the "
                    f"HTML reads “{p[0]}” without it."
                    if gone else
                    f"Percent sign — at this point the PDF reads “{p[0]}” but the "
                    f"HTML reads “{p[0]}%”.",
                )
            elif p[3] and h[4] and p[3] != h[4]:
                sym_hits += 1
                yield (
                    "currency",
                    "error",
                    f"{p[0]}: PDF “{p[3]}” / HTML “{h[4]}”",
                    f"Currency — at this point the PDF shows {p[3]}{p[0]} but the "
                    f"HTML shows {h[4]}{p[0]}. Every digit matches, so no value "
                    f"check can see it; confirm the symbol against the PDF. PDF "
                    f"context: “{p[1]}”.",
                )
            if sym_hits >= 25:
                break
        if sym_hits >= 25:
            break

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

    def _in_table(j: int) -> bool:
        """Whether HTML position *j* is a bare figure in a grid cell."""
        if not html_seq:
            return False
        k = min(max(j, 0), len(html_seq) - 1)
        return html_seq[k][2]

    individual = 0
    bulk = {"replace": 0, "delete": 0, "insert": 0}
    reflowed = 0
    prose_moves = 0
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if idx in reflow:
            reflowed += max(i2 - i1, j2 - j1)
            continue
        span = max(i2 - i1, j2 - j1)
        # A pure insertion or deletion inside PROSE is usually reflow or PDF
        # extraction damage rather than a changed value: paragraphs are rewrapped
        # between the two renderings, and pdfplumber routinely runs a sentence's
        # words together ("OnApril9,2024,IASBha") or interleaves a two-column
        # standards table, either of which reorders the paragraph's numbers while
        # every value is still present.  Those are counted in the summary instead
        # of itemised, so the itemised list stays about values rather than layout.
        #
        # A SUBSTITUTION is itemised wherever it occurs — that is the "PDF reads
        # 3, HTML reads 8" case this module exists for, and it is meaningful in
        # prose as much as in a table.  Table cell order IS preserved by both
        # renderings, so inserts and deletes are itemised there too.
        if tag != "replace" and not _in_table(j1):
            prose_moves += span
            continue
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

    if prose_moves:
        yield (
            "seq-prose-order",
            "review",
            f"{prose_moves} numbers in prose sit at a different sequence position",
            f"Number sequence in prose — {prose_moves} numbers inside paragraphs "
            "appear at a different point in the HTML's number sequence than in the "
            "PDF's, with no value substituted (every value is still present). In "
            "running text this is usually rewrapping, or PDF extraction that ran a "
            "sentence's words together or interleaved a two-column table, rather "
            "than a changed figure — so these are counted here rather than listed "
            "one by one. The count is shown so the residue stays visible: if it is "
            "large relative to the document, the prose sections deserve a read "
            "against the PDF. Substituted values are always itemised separately.",
        )
