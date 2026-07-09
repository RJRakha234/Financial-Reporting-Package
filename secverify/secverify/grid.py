"""Phase 2 (toolbeta): geometry-based table-grid cell comparison.

Borderless financial tables defeat pdfplumber's ``extract_tables``, so the
PDF grid is rebuilt from word geometry (cluster words into rows by their
vertical position; figures keep their left-to-right order).  The HTML grid
comes straight from ``<table><tr><td>``.  The two grids are then aligned —
rows by label *sequence* (so short/duplicate labels are disambiguated by
position, not uniqueness) — and compared cell by cell.

The whole thing is behind a **confidence gate**: a table is compared only
when its rows align cleanly (most labels match in order); otherwise it is
skipped and the base text checks apply.  Asserting only on confidently
aligned tables is what keeps this false-positive-free.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .numbers import iter_tokens
from .textnorm import canonical

#: a table must have at least this many figure-bearing rows to be treated as
#: a financial statement worth grid-comparing
MIN_STATEMENT_ROWS = 5
#: minimum share of HTML rows that must align to a PDF row before ANY cell in
#: the table is compared (the confidence gate)
MIN_ALIGN_RATIO = 0.85
#: a row pair is only compared when the labels match at least this well
MIN_ROW_LABEL_RATIO = 0.95
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


def _fig_keys(text: str) -> list[str]:
    """Significant figure keys in *text*, in order, minus note-refs, ids and
    dates (a date's day/year would otherwise read as figures)."""
    text = _DATE_RE.sub(" ", text)
    out = []
    for _s, _e, tok, key in iter_tokens(text):
        t = tok.strip()
        if re.match(r"^\(?0\d", t):
            continue  # leading-zero identifier
        if re.fullmatch(r"\d{1,2}\.\d{1,2}", key):
            continue  # note reference like 2.15
        out.append(key)
    return out


def _parse_html_rows(table) -> list[tuple[str, list[str]]]:
    """``[(label, [figure keys in column order])]`` for a HTML table."""
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            continue
        label = ""
        figs: list[str] = []
        for cell in cells:
            ctext = cell.get_text(" ", strip=True)
            if not label and re.search(r"[A-Za-z]{3,}", ctext):
                label = ctext
            figs.extend(_fig_keys(ctext))
        rows.append((canonical(label, letters_only=True), figs))
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
    """Word lists per page across all reference PDFs."""
    import pdfplumber

    pages = []
    for path in pdf_paths:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_words(x_tolerance=1))
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
            figs = _fig_keys(line)
            lbl = canonical(_DATE_RE.sub(" ", re.sub(r"[\d,()%₹$.\-]+", " ", line)),
                            letters_only=True)
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
                    and SequenceMatcher(None, hlbl, plbl).ratio() >= 0.95
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
    for html_rows in html_tables:
        for hlbl, hfigs in html_rows:
            n = len(hfigs)
            if len(hlbl) < 12 or "refertonote" in hlbl:
                continue
            if n in ALLOWED_FIG_COUNTS:
                severity, wide = "error", False
            elif n in WIDE_FIG_COUNTS:
                severity, wide = "caution", True
            else:
                continue
            cands = [c for c in candidates(hlbl) if len(c) == n]
            if not cands:
                continue  # label not locatable in the PDF → base checks apply
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
