"""Phase 2 (toolbeta): geometry-based table-grid cell comparison.

Borderless financial tables defeat pdfplumber's ``extract_tables``, so the
PDF grid is rebuilt from word geometry (cluster words into rows by their
vertical position; figures keep their left-to-right order).  The HTML grid
comes straight from ``<table><tr><td>``.  The two grids are then aligned —
rows by label *sequence* (so short/duplicate labels are disambiguated by
position, not uniqueness) — and compared cell by cell.

Findings are **tiered by confidence, never suppressed by it**.  A long-labelled
row at primary-statement width is a hard ``error``; a shorter label, an unusual
column count, or a position that could only be matched outside the table's own
region is reported at the ``review``/``caution`` tier instead.  What a low
confidence must never do is silence the check: a check that quietly declines is
indistinguishable from a check that passed, and that is the one outcome able to
hide a real error.  Rows the grid genuinely cannot compare (the PDF splits the
columns differently, the label is too generic to locate) are therefore counted
and named in the *unchecked ledger* emitted at the end of :func:`grid_compare`,
so the residue needing human eyes is explicit rather than invisible.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from .numbers import iter_tokens
from .textnorm import canonical

#: a table must have at least this many figure-bearing rows to be treated as
#: a financial statement worth grid-comparing
MIN_STATEMENT_ROWS = 5
#: fuzzy-match threshold for pairing a HTML row label with a PDF one when no
#: exact canonical match exists (tolerates parsing variation)
MIN_ROW_LABEL_RATIO = 0.95
#: min label length (letters) for a row to join the row-ORDER sequence.
#: Long enough to exclude ultra-generic labels ("total") that could mis-
#: anchor, short enough to include real line items like "Hi-Tech"/"Retail"
#: so an adjacent-row swap (Life Sciences <-> Hi-Tech) is caught.
ROW_ORDER_MIN_LABEL = 6
#: min label length (letters) for a row to be VALUE-compared at all.  Rows with
#: a longer label (>= 12) are compared as hard errors; 6-11 letters are compared
#: at the review tier.  Below this a label ("total", "net") cannot be located in
#: the PDF with any confidence, so the row goes to the unchecked ledger — it is
#: never dropped in silence.
ROW_VALUE_MIN_LABEL = 6
#: primary-statement rows — 2 periods (optionally a note-ref column already
#: stripped).  A mismatch here is reported as a hard "error" (red).
ALLOWED_FIG_COUNTS = (2, 3)
#: wider movement/roll-forward matrices (changes in equity, PP&E, tax
#: reconciliations).  The same "no counterpart anywhere" rule is zero-false-
#: positive on these too, but wide cross-tabulated tables are harder to parse,
#: so a mismatch is reported at the lower-confidence "caution" tier (brown)
#: rather than as a hard error.
WIDE_FIG_COUNTS = (4, 5, 6, 7)

_DATE_RE = re.compile(
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+(?:19|20)\d{2}|\b(?:19|20)\d{2}\b"
)


#: labels whose values are per-share / ratio amounts — these look exactly like
#: note references ("62.40" vs "2.15"), so on these rows the note-ref filter is
#: turned OFF, otherwise EPS / DPS / face-value figures vanish from the checks.
_PER_SHARE_RE = re.compile(
    r"per\s+share|per\s+equity\s+share|earnings\s+per|dividend\s+per|"
    r"book\s+value\s+per|per\s+unit|face\s+value|nominal\s+value|par\s+value",
    re.I,
)

#: a table's PERIOD-HEADER row, matched against the canonical letters-only label.
#: "3 months ended June 30, 2025" canonicalises to "monthsendedjune" and its only
#: figure is the "3" of "3 months" — a column caption, not a data row.  Comparing
#: it as data invents a row whose label is nowhere in the PDF.  Period headers are
#: owned by the DATE check (see annotate._date_mismatch), which compares the
#: calendar dates properly, so skipping them here loses no coverage.
_PERIOD_HEADER_RE = re.compile(
    r"^(?:three|six|nine|twelve|half)?"
    r"(?:months?|quarters?|years?|periods?|halfyears?)ended"
    r"|^as(?:at|of)|^particulars$",
)


def _is_period_header(label: str, figs: list[str]) -> bool:
    """Whether a row is a period/column caption rather than a data row.

    Requires BOTH the caption wording and figures that are bare 1-3 digit
    integers (the "3" of "3 months ended"), so a real data row is never mistaken
    for a header just because its label begins with a period phrase.
    """
    if not _PERIOD_HEADER_RE.match(label):
        return False
    return all(re.fullmatch(r"\d{1,3}", f) for f in figs)


def _fig_keys(text: str, strip_note_refs: bool = True) -> list[str]:
    """Significant figure keys in *text*, in order, minus ids and dates.

    Two-decimal values like ``2.15`` are dropped as note references *unless*
    ``strip_note_refs`` is False — on a per-share/ratio row they are real
    figures (EPS ``62.40``) and must be kept."""
    text = _DATE_RE.sub(" ", text)
    out = []
    for _s, _e, tok, key in iter_tokens(text):
        t = tok.strip()
        if re.match(r"^\(?0\d", t):
            continue  # leading-zero identifier
        if strip_note_refs and re.fullmatch(r"\d{1,2}\.\d{1,2}", key):
            continue  # note reference like 2.15
        out.append(key)
    return out


def line_label_figs(line: str) -> tuple[str, list[str]]:
    """``(canonical label, figure keys)`` for a raw text line, keeping
    per-share decimals that would otherwise read as note references."""
    keep = bool(_PER_SHARE_RE.search(line))
    lbl = canonical(
        _DATE_RE.sub(" ", re.sub(r"[\d,()%₹$.\-]+", " ", line)), letters_only=True
    )
    return lbl, _fig_keys(line, strip_note_refs=not keep)


def cells_label_figs(cell_texts) -> tuple[str, list[str]]:
    """``(canonical label, figure keys)`` for a table row's cell texts, with the
    same per-share awareness as :func:`line_label_figs`."""
    label = ""
    for t in cell_texts:
        if not label and re.search(r"[A-Za-z]{3,}", t):
            label = t
    keep = bool(_PER_SHARE_RE.search(label))
    figs: list[str] = []
    for t in cell_texts:
        figs.extend(_fig_keys(t, strip_note_refs=not keep))
    return canonical(label, letters_only=True), figs


def _parse_html_rows(table) -> list[tuple[str, list[str]]]:
    """``[(label, [figure keys in column order])]`` for a HTML table."""
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        rows.append(cells_label_figs([c.get_text(" ", strip=True) for c in cells]))
    return rows


def _parse_pdf_rows(words) -> list[tuple[str, list[str]]]:
    """Rebuild ``[(label, [figures left-to-right])]`` from PDF word geometry."""
    lines: dict[int, list] = {}
    for w in words:
        band = round(w["top"] / 3.0)  # cluster by vertical band (~3px)
        lines.setdefault(band, []).append(w)
    rows = []
    for band in sorted(lines):
        ws = sorted(lines[band], key=lambda w: w["x0"])
        label_parts, figs = [], []
        for w in ws:
            if re.search(r"[A-Za-z]{2,}", w["text"]):
                label_parts.append(w["text"])
            else:
                figs.extend(_fig_keys(w["text"]))
        rows.append((canonical(" ".join(label_parts), letters_only=True), figs))
    return rows


def _is_statement_table(html_rows) -> bool:
    labelled = sum(1 for lbl, f in html_rows if f and len(lbl) >= 4)
    return labelled >= MIN_STATEMENT_ROWS


def _load_pdf_pages(pdf_paths):
    """Word lists per page across all reference PDFs.

    Geometry is an enhancement layer: a path that cannot be opened (a unit
    test's placeholder, a moved file) is skipped rather than fatal, so the
    text-based checks still run.
    """
    import pdfplumber

    pages = []
    for path in pdf_paths or []:
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    pages.append(page.extract_words(x_tolerance=1))
        except Exception:
            continue
    return pages


def grid_compare(corpus, soup, pdf_paths):
    """Yield ``(kind, severity, excerpt, remark)`` for grid cell mismatches.

    A HTML statement row is flagged only when its exact ``(label, ordered
    figures)`` has **no counterpart anywhere in the PDF's rows** — which is
    immune to row/page mis-alignment (a value that is merely mis-paired in the
    comparison still exists correctly elsewhere and is not flagged).  Because
    the counterpart is sought against *every* PDF row carrying the label, this
    holds for repeated labels too ("total", "government securities", a line
    item's current and non-current portions), so those rows are checked
    without the false positives that positional table alignment produced.
    """
    html_tables = [
        r
        for r in (_parse_html_rows(t) for t in soup.find_all("table"))
        if _is_statement_table(r)
    ]
    if not html_tables or not pdf_paths:
        return

    # PDF rows from pdfplumber's own line clustering (more reliable than
    # ad-hoc word banding): each text line → (label, figures).
    pdf_rows: list[tuple[str, list[str]]] = []
    for raw in corpus.pages_raw:
        for line in raw.splitlines():
            if not re.search(r"[A-Za-z]{3,}", line) or not re.search(r"\d", line):
                continue
            lbl, figs = line_label_figs(line)
            if lbl and figs:
                pdf_rows.append((lbl, figs))
    pdf_by_label: dict[str, list[list[str]]] = {}
    for lbl, f in pdf_rows:
        pdf_by_label.setdefault(lbl, []).append(f)

    def candidates(hlbl: str) -> list[list[str]]:
        """Figure tuples of every PDF row whose label matches *hlbl* — exact
        first, else a close fuzzy match (tolerates parsing variation)."""
        cands = list(pdf_by_label.get(hlbl, []))
        if not cands:
            for plbl, pf in pdf_rows:
                if (
                    abs(len(plbl) - len(hlbl)) <= 3
                    and SequenceMatcher(None, hlbl, plbl).ratio()
                    >= MIN_ROW_LABEL_RATIO
                ):
                    cands.append(pf)
        return cands

    # A row is flagged only when its (label, figures) has NO counterpart
    # anywhere in the PDF.  That immunity holds for a REPEATED label too: a
    # line item that recurs (its current and non-current portions, a "total"
    # in several schedules) is checked against *all* PDF rows carrying that
    # label, so the correct value — which always appears against the label
    # somewhere — is never flagged, while a value that appears against the
    # label nowhere is.  This is what lets repeated rows be checked without the
    # false positives that per-table positional alignment produced (a coarse
    # region merges sub-tables whose same-named rows hold different figures).
    emitted: set[tuple] = set()
    #: rows the grid check could not compare, tallied by reason so they are
    #: reported as a visible inventory instead of vanishing (see the ledger
    #: yielded below).  A skipped check must never be an invisible one.
    unchecked: Counter = Counter()
    unchecked_egs: dict[str, list[str]] = {}

    def _note_unchecked(reason: str, label: str) -> None:
        unchecked[reason] += 1
        egs = unchecked_egs.setdefault(reason, [])
        if len(egs) < 5 and label not in egs:
            egs.append(label)

    for html_rows in html_tables:
        for hlbl, hfigs in html_rows:
            n = len(hfigs)
            if not hlbl or not n or "refertonote" in hlbl:
                continue
            if _is_period_header(hlbl, hfigs):
                continue  # a column caption — owned by the date check
            if len(hlbl) < ROW_VALUE_MIN_LABEL:
                # a 1-5 letter label ("total", "net") cannot be located in the
                # PDF with any confidence — record it rather than drop it
                _note_unchecked("label too short to locate", hlbl)
                continue
            # Confidence TIERING (not suppression).  A long label at primary-
            # statement width is a hard error; a shorter label, or an unusual
            # column count, is surfaced at the review tier.  Previously both of
            # those were skipped outright, so a wrong value on a short-labelled
            # row ("Hi-Tech", "Retail") produced no finding at all.
            if len(hlbl) >= 12 and n in ALLOWED_FIG_COUNTS:
                severity, wide = "error", False
            elif len(hlbl) >= 12 and n in WIDE_FIG_COUNTS:
                severity, wide = "caution", True
            else:
                severity, wide = "review", n >= min(WIDE_FIG_COUNTS)
            cands = [c for c in candidates(hlbl) if len(c) == n]
            if not cands:
                # No PDF row carries this label with this many figures.  Two
                # very different situations, and conflating them is what made
                # this branch silent:
                #   * the label is nowhere in the PDF at all — the row may be
                #     fabricated or renamed, so flag it;
                #   * the label IS there but split across a different number of
                #     columns — a parsing difference, not an error, so record it
                #     in the ledger rather than raise a flag on every such row.
                if candidates(hlbl):
                    _note_unchecked("column count differs from the PDF", hlbl)
                elif hlbl in corpus.letters.canon:
                    # The label IS in the PDF, just not on any single print
                    # line: a long row label ("Liquid mutual fund units carried
                    # at fair value through profit or loss") wraps across two or
                    # three lines, so the per-line PDF grid never holds it
                    # whole.  That is a layout difference, not a missing row —
                    # flagging it would put a red-adjacent finding on every long
                    # label in the document.
                    _note_unchecked("label wraps across PDF lines", hlbl)
                else:
                    sig = (hlbl, ("__unlocated__",))
                    if sig not in emitted:
                        emitted.add(sig)
                        yield (
                            "grid-row-unlocated",
                            "review",
                            f"{hlbl}: {' '.join(hfigs)} — label not found in the PDF",
                            f"Row not located — no line in the PDF carries the "
                            f"label “{hlbl}”, so its figures ({', '.join(hfigs)}) "
                            "could not be verified against a counterpart. The row "
                            "may have been renamed, merged, or added. Confirm it "
                            "exists in the source.",
                        )
                continue
            if any(c == hfigs for c in cands):
                continue  # some PDF occurrence matches exactly → correct
            sig = (hlbl, tuple(hfigs))
            if sig in emitted:
                continue
            emitted.add(sig)
            pfigs = cands[0]
            wide_note = (
                " This is a wide movement matrix (harder to parse), so it is "
                "flagged as a lower-confidence check — verify rather than assume."
                if wide else ""
            )
            if any(sorted(c) == sorted(hfigs) for c in cands):
                yield (
                    "grid-column-order",
                    severity,
                    f"{hlbl}: HTML {' '.join(hfigs)} / PDF {' '.join(pfigs)}",
                    f"Table column order — row “{hlbl}” has the same figures "
                    f"in a different order: HTML {', '.join(hfigs)} vs PDF "
                    f"{', '.join(pfigs)}. The comparative columns may be "
                    f"transposed.{wide_note}",
                )
            else:
                yield (
                    "grid-value",
                    severity,
                    f"{hlbl}: HTML {' '.join(hfigs)} / PDF {' '.join(pfigs)}",
                    f"Table cell value — row “{hlbl}” shows {', '.join(hfigs)} "
                    f"in the HTML but no PDF row carrying that label has those "
                    f"figures (closest: {', '.join(pfigs)}). Verify this "
                    f"row.{wide_note}",
                )

    # Row ORDER within a table.  Rows reordered inside a statement keep every
    # value correct — presence, row-value and even footing all pass (same rows,
    # same total) — so sequence is the only signal.  Each table is pinned to a
    # PDF region by its unique-label rows; every row whose label occurs exactly
    # once in that region must then appear in the PDF's order.  Rows outside
    # the longest increasing subsequence are reported for review.
    from .coverage import _lis_indices

    hcount = Counter(l for rows in html_tables for l, f in rows if l and f)
    pdf_pos: dict[str, list[int]] = {}
    for i, (l, f) in enumerate(pdf_rows):
        pdf_pos.setdefault(l, []).append(i)
    order_emitted: set[tuple] = set()
    for html_rows in html_tables:
        anchors = [
            l for l, f in html_rows
            if l and f and hcount[l] == 1
            and len(pdf_pos.get(l, [])) == 1 and len(l) >= 10
        ]
        if len(anchors) < 2:
            # the table cannot be pinned to a PDF region, so its row ORDER is
            # not verified at all — record that rather than pass over it
            _note_unchecked(
                "row order unverified (table not locatable in the PDF)",
                next((l for l, f in html_rows if l and f), "?"),
            )
            continue
        apos = sorted(pdf_pos[l][0] for l in anchors)
        lo, hi = apos[0] - 8, apos[-1] + 8
        table_label_count = Counter(l for l, f in html_rows if l and f)
        seq: list[tuple[str, int, list[str]]] = []
        for l, f in html_rows:
            if not (l and f) or len(l) < ROW_ORDER_MIN_LABEL or "refertonote" in l:
                continue
            if _is_period_header(l, f):
                continue  # a column caption, not a row in the sequence
            if table_label_count[l] != 1:
                continue  # label repeats in this table (dates strip out of
                #           SOCIE balance rows) — ambiguous, skip
            in_win = [p for p in pdf_pos.get(l, []) if lo <= p <= hi]
            if len(in_win) != 1:
                _note_unchecked("row position ambiguous in the PDF region", l)
                continue
            seq.append((l, in_win[0], f))
        # two rows sharing one PDF position are the same ambiguity — drop both
        pos_count = Counter(p for _l, p, _f in seq)
        for e in seq:
            if pos_count[e[1]] != 1:
                _note_unchecked("row position ambiguous in the PDF region", e[0])
        seq = [e for e in seq if pos_count[e[1]] == 1]
        if len(seq) < 3:
            _note_unchecked(
                "row order unverified (too few locatable rows)",
                next((l for l, f in html_rows if l and f), "?"),
            )
            continue
        keep = _lis_indices([p for _l, p, _f in seq])
        for idx, (l, p, f) in enumerate(seq):
            if idx in keep or (l, tuple(f)) in order_emitted:
                continue
            order_emitted.add((l, tuple(f)))
            yield (
                "grid-row-order",
                "review",
                f"{l}: HTML {' '.join(f)} / PDF row order differs",
                f"Row order — “{l}” (values {', '.join(f)}, all correct) sits "
                "in a different position within this table than in the PDF. "
                "Rows reordered inside a statement pass every value check, so "
                "verify the row sequence against the PDF.",
            )

    # Duplicate-label VALUE reassignment (geometry alignment).  Two rows that
    # share a label — "Balance as at April 1" appearing twice, income-tax-
    # assets in a current AND a non-current section — each hold their own
    # values.  The text checks accept a swap between them because each swapped
    # row still matches the OTHER occurrence somewhere ("present anywhere").
    # PDF word geometry pins each occurrence to its row position, so a swap is
    # visible.  Only the exact swap shape is flagged: the SAME set of value
    # tuples reassigned to different positions.  If the value set differs it is
    # left to the checks above; if the order already matches it is correct — so
    # a legitimate current/non-current repeat never false-flags.
    geom_rows = [
        r
        for page in _load_pdf_pages(pdf_paths)
        for r in _parse_pdf_rows(page)
        if r[0] and r[1] and len(r[0]) >= 6
    ]
    yield from _row_swap_findings(html_tables, geom_rows)
    yield from _row_sequence_findings(html_tables, geom_rows)
    yield from _transpose_order_findings(html_tables, geom_rows)

    # The unchecked LEDGER.  Everything the grid checks could not compare is
    # reported here as a single visible item, broken down by reason with
    # examples.  A check that quietly declines is indistinguishable from a check
    # that passed, which is the one failure mode that can hide a real error —
    # so the declines are counted and named.
    if unchecked:
        total = sum(unchecked.values())
        lines = "; ".join(
            f"{reason}: {cnt} (e.g. {', '.join(unchecked_egs.get(reason, [])[:3])})"
            for reason, cnt in unchecked.most_common()
        )
        yield (
            "grid-unchecked-ledger",
            "review",
            f"{total} table rows could not be grid-verified",
            f"Not verified by the table-grid checks — {total} rows, by reason: "
            f"{lines}. These are neither passes nor failures: the grid check "
            "could not compare them against the PDF, so nothing is asserted "
            "about them. The text-level checks still apply to these rows; treat "
            "this list as the residue that needs eyes.",
        )


#: two matched PDF rows more than this many rows apart belong to different
#: table regions (e.g. a combined exhibit's condensed vs annual sections)
_REGION_GAP = 30


def _dominant_region(sorted_positions: list[int]) -> tuple[int, int]:
    """``(lo, hi)`` of the largest run of positions with no gap > _REGION_GAP."""
    if not sorted_positions:
        return (0, -1)
    best = (sorted_positions[0], sorted_positions[0])
    run_start = prev = sorted_positions[0]
    best_len = 1
    cur_len = 1
    for p in sorted_positions[1:]:
        if p - prev > _REGION_GAP:
            run_start = p
            cur_len = 1
        else:
            cur_len += 1
        if cur_len > best_len:
            best_len = cur_len
            best = (run_start, p)
        prev = p
    return best


#: a leading footnote/reference marker on a row label — "Life Sciences (4)".
#: In the HTML it sits in the label cell and parses as the row's first figure;
#: in the PDF it is a SUPERSCRIPT, so it lands in a different vertical band and
#: is not part of the geometry row at all.  The composite (label, values) key
#: then never matches and the row drops out of the order check silently.
#: Observed on a real segment note: every footnote-marked segment — Financial
#: Services, Retail, Communication, Life Sciences, All other segments — was
#: excluded, while unmarked Hi-Tech and Manufacturing matched. A swap between a
#: marked and an unmarked segment was therefore invisible.
def _strip_marker(figs: tuple) -> tuple:
    """Drop a leading single-digit footnote marker from a row's figures."""
    out = list(figs)
    while out and re.fullmatch(r"\d", out[0]):
        out.pop(0)
    return tuple(out)


def _row_sequence_findings(html_tables, geom_rows):
    """Row-ORDER check keyed on (label, values), from PDF word geometry.

    A segment statement lists each segment TWICE — once under "Revenue by
    business segment", again under "Segment profit" — so the label alone
    ("Hi-Tech") is not unique and the label-only order check skips it.  But
    the (label, values) pair IS unique — "Hi-Tech" revenue (3,710 …) differs
    from "Hi-Tech" profit (911 …) — so keying on the pair disambiguates the
    occurrences, and a swap of two rows inside one sub-section (Life Sciences
    ↔ Hi-Tech) is caught.  Each such row is located at its single matching
    PDF position; rows out of the longest increasing subsequence are flagged.
    Only rows whose (label, values) pair is unique in BOTH the HTML table and
    the whole PDF take part, so a coincidental repeat never mis-pairs.
    """
    if not geom_rows:
        return
    from .coverage import _lis_indices

    # Key on the marker-stripped figures so a footnote superscript cannot
    # decide whether a row is order-checked.  Stripping is applied to BOTH
    # sides, so a collision it might create simply makes the row non-unique and
    # the existing uniqueness guards skip it — never a mismatch.
    gpos: dict[tuple, list[int]] = {}
    for i, (l, f) in enumerate(geom_rows):
        gpos.setdefault((l, _strip_marker(tuple(f))), []).append(i)
    emitted: set[tuple] = set()
    for html_rows in html_tables:
        hrows = [
            (l, _strip_marker(tuple(f)))
            for l, f in html_rows if l and f and len(l) >= 6
        ]
        hcnt = Counter(hrows)
        seq: list[tuple[int, tuple]] = []
        for key in hrows:
            if hcnt[key] != 1:
                continue  # identical row twice in the HTML → ambiguous
            g = gpos.get(key)
            if not g or len(g) != 1:
                continue  # not uniquely locatable in the PDF
            seq.append((g[0], key))
        if len(seq) < 4:
            continue
        # Restrict to the table's DOMINANT contiguous PDF region.  In a
        # combined exhibit one HTML table's rows can match geometry rows in
        # two far-apart sections (condensed AND annual); those cross-section
        # jumps are not a reordering.  Cluster the matched positions by gap
        # and keep only the largest run, so the sequence check sees one table
        # region, not the leap between sections.
        lo, hi = _dominant_region(sorted(g for g, _k in seq))
        seq = [(g, key) for g, key in seq if lo <= g <= hi]
        if len(seq) < 4:
            continue
        # Flag rows that fall out of the longest increasing subsequence — a
        # reordered row, at ANY distance (a full row moved from the 16th line
        # to the 2nd).  The one shape that must NOT flag is a lone row whose
        # single PDF match lands in a DIFFERENT sub-table beyond this one (the
        # "service cost" coincidence): guard on the displaced row's position
        # lying WITHIN the span of the rows that stayed in order — a genuine
        # interior move does; a spurious edge match sits beyond the span.
        pos = [g for g, _k in seq]
        keep = set(_lis_indices(pos))
        kept = [pos[i] for i in keep]
        if not kept:
            continue
        lo_k, hi_k = min(kept), max(kept)
        table_keys = set(hrows)  # every (label, values) in this HTML table
        for idx, (g, key) in enumerate(seq):
            if idx in keep or key in emitted:
                continue
            # Confidence, not suppression.  A displaced row inside the in-order
            # span is a clear reordering.  Beyond that span it is either a
            # genuine move of the first/last row to the opposite end OR a
            # spurious match in a different sub-table; the neighbours tell them
            # apart (a row that truly belongs here has its PDF neighbour also
            # present in this HTML table).  When the neighbours are foreign the
            # finding is WEAKER — but it is still reported, at a lower tier,
            # because staying silent is the one outcome that can hide a real
            # move.  Only the wording changes, never the visibility.
            weak = False
            if not (lo_k <= g <= hi_k):
                nbrs = set()
                if g > 0:
                    nl, nf = geom_rows[g - 1]
                    nbrs.add((nl, _strip_marker(tuple(nf))))
                if g + 1 < len(geom_rows):
                    nl, nf = geom_rows[g + 1]
                    nbrs.add((nl, _strip_marker(tuple(nf))))
                weak = not (nbrs & table_keys)
            emitted.add(key)
            lbl, figs = key
            if weak:
                yield (
                    "grid-row-order-weak",
                    "caution",
                    f"{lbl}: {' '.join(figs)} — position could not be confirmed",
                    f"Row order (low confidence) — the row “{lbl}” (values "
                    f"{', '.join(figs)}) matched a PDF row that sits outside "
                    "this table's own region, among unrelated rows. That is "
                    "usually a coincidental match rather than a moved row, so "
                    "this is reported as a caution rather than an error — but "
                    "its position could not be positively confirmed, so check "
                    "that this row sits where the PDF puts it.",
                )
            else:
                yield (
                    "grid-row-order",
                    "review",
                    f"{lbl}: {' '.join(figs)} out of sequence",
                    f"Row order — the row “{lbl}” (values {', '.join(figs)}, all "
                    "correct) sits in a different position within this table than "
                    "in the PDF; a segment or line item may have been reordered. "
                    "Rows moved inside a statement pass every value check, so "
                    "verify the sequence against the PDF.",
                )


def _row_swap_findings(html_tables, geom_rows):
    """Duplicate-label value reassignments, from PDF word-geometry rows.

    A label that occurs more than once within one HTML table ("Income tax
    assets" current AND non-current, "Balance as at April 1" for two years)
    holds its own values.  The text checks accept a swap between the
    occurrences because each swapped row still matches the OTHER occurrence
    somewhere.  Geometry pins each occurrence to its row, so a swap is
    visible.  Flagged only in the exact swap shape — the SAME multiset of
    value tuples reassigned to different positions — so a value SET that
    genuinely differs is left to the other checks, and a repeat already in
    the right order never false-flags.
    """
    if not geom_rows:
        return
    geom_by_label: dict[str, list[list[str]]] = {}
    for lbl, f in geom_rows:
        geom_by_label.setdefault(lbl, []).append(f)
    swap_emitted: set[str] = set()
    for html_rows in html_tables:
        hrows = [(l, f) for l, f in html_rows if l and f and len(l) >= 12]
        hcnt = Counter(l for l, f in hrows)
        for L, c in hcnt.items():
            if c < 2 or L in swap_emitted:
                continue
            html_occ = [tuple(f) for l, f in hrows if l == L]
            pdf_occ = [tuple(f) for f in geom_by_label.get(L, [])]
            # only compare when the occurrence counts line up exactly (a
            # different count = a different table shape → not comparable)
            if len(pdf_occ) != len(html_occ):
                continue
            if sorted(html_occ) != sorted(pdf_occ):
                continue  # value SET differs → owned by the checks above
            if html_occ == pdf_occ:
                continue  # same values, same order → correct
            for h, p in zip(html_occ, pdf_occ):
                if h != p:
                    swap_emitted.add(L)
                    yield (
                        "grid-row-swap",
                        "review",
                        f"{L}: HTML {' '.join(h)} vs PDF {' '.join(p)}",
                        f"Row values reassigned — “{L}” appears more than "
                        "once and the same set of values is present, but "
                        "attached to different occurrences than in the PDF "
                        f"(here the HTML shows {', '.join(h)} where the PDF "
                        f"has {', '.join(p)}). The figures all exist, so the "
                        "other checks stay silent — verify each value sits "
                        "against the correct occurrence of this row.",
                    )
                    break


#: a transposed matrix must carry at least this many figures before an out-of-
#: order permutation is trusted as a real reordering.  Short sequences permute
#: by coincidence; 6+ significant figures forming a perfect permutation do not.
_TRANSPOSE_MIN_LEN = 6


def _transpose_order_findings(html_tables, geom_rows):
    """Segment order across a TRANSPOSED matrix.

    Some exhibits print the segment schedule as a wide matrix — segments across
    the COLUMNS, one metric per ROW ("Revenue from operations 49,908 29,078 …",
    "Segment profit 12,678 …") — while the HTML lists each segment as its own
    ROW.  Neither the row-order nor the column-order check aligns across that
    transpose, so a swap of two segments (Life Sciences ↔ Hi-Tech) stayed
    invisible: every value is present and correct, only the sequence changed.

    This closes it.  Each wide PDF row (≥ 6 figures) is a segment sequence in
    column order; each HTML table column, read top to bottom, is the same
    segment sequence in row order.  When an HTML column is an EXACT permutation
    of a wide PDF row — the identical multiset of values — but in a different
    order, the segments are transposed out of sequence.  Requiring an exact
    multiset match of 6+ significant figures is what keeps this false-positive-
    free: an unrelated column is astronomically unlikely to be a perfect
    permutation of a wide matrix row, and a correctly-ordered matrix matches
    value-for-value so it never flags.
    """
    if not geom_rows:
        return
    wide = [(l, [str(x) for x in f]) for l, f in geom_rows if len(f) >= _TRANSPOSE_MIN_LEN]
    if not wide:
        return
    # index wide PDF rows by their value multiset so a matching HTML column is
    # found directly rather than by scanning every pair
    wide_by_key: dict[tuple, list[tuple[str, list[str]]]] = {}
    for l, f in wide:
        wide_by_key.setdefault(tuple(sorted(f)), []).append((l, f))

    emitted: set[tuple] = set()
    for html_rows in html_tables:
        data = [[str(x) for x in f] for _l, f in html_rows if f]
        if len(data) < _TRANSPOSE_MIN_LEN:
            continue
        ncols = max(len(f) for f in data)
        for c in range(ncols):
            col = [f[c] for f in data if len(f) > c]
            if len(col) < _TRANSPOSE_MIN_LEN:
                continue
            # a real segment matrix carries thousands/crores — ignore columns
            # of tiny values (footnote refs, counts) that could permute by luck
            if not any(len(re.sub(r"\D", "", v)) >= 4 for v in col):
                continue
            key = tuple(sorted(col))
            for plbl, pf in wide_by_key.get(key, []):
                if col == pf:
                    continue  # same order → correct
                sig = (plbl, key)
                if sig in emitted:
                    continue
                emitted.add(sig)
                yield (
                    "grid-transpose-order",
                    "review",
                    f"{plbl}: HTML column {' '.join(col)} / PDF row {' '.join(pf)}",
                    f"Segment order (transposed matrix) — the PDF prints “{plbl}” "
                    "as a wide row with the segments across the columns, while "
                    "the HTML lists each segment as a row. Every value is present "
                    f"and correct, but the sequence differs: the HTML reads "
                    f"{', '.join(col)} down the column where the PDF row reads "
                    f"{', '.join(pf)}. Two segments may have been swapped — verify "
                    "the segment order against the PDF.",
                )
                break
