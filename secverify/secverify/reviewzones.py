"""Blue manual-review overlay for Class 2 / Class 3 error surfaces.

The engine cannot *verdict* two error families, because they preserve every
token in the document (nothing is missing to react to):

* **Class 2 — token-preserving rearrangements**: two figures swapped in running
  prose, a value tied to the wrong label where that pair also exists elsewhere.
* **Class 3 — wrong cross-reference**: "refer Note 12" changed to "Note 21"
  when both notes exist.

For a reviewer who does not want to take *any* chance on these, this overlay
paints the *locations where such an error could hide* — not an error claim, a
"verify this by eye" marker — in **blue**:

* every **cross-reference phrase** ("refer to Note 12", "see Schedule 3");
* every **figure that sits in prose** (outside a ``<table>``), validated by
  presence only, so a prose swap would pass;
* every figure in an **in-table comparative row the tool could not positively
  confirm** — a real line-item row (2–3 figures) whose exact ``(label, ordered
  figures)`` was *not* found in the PDF.  Most rows confirm and stay green; the
  ones that do not are exactly where a value swapped between two items can hide
  (e.g. a small note table where the correct pairing also appears elsewhere, so
  the strict checks stay silent).

Blue never overrides a red finding: a genuine discrepancy stays red.
"""

from __future__ import annotations

import re

from bs4 import NavigableString

from .numbers import is_significant, iter_tokens
from .textnorm import canonical
from .grid import _fig_keys, _DATE_RE, line_label_figs, cells_label_figs

#: label tokens that mark signature / heading furniture, not a line item
_FURNITURE_RE = re.compile(
    r"bengaluru|july|place|date|udin|membership|particulars|refertonote|"
    r"condensedconsolidated|represents|forandonbehalf|charteredaccountants|"
    r"firmsregistration|secretary|director|chairman|partner",
    re.I,
)

_NOTE_REF_RE = re.compile(r"^\(?\d{1,2}\.\d{1,2}\)?$")


def _paint_blue(span, title: str) -> None:
    """Reclass a green figure span to blue, tolerating a str or list class."""
    cur = span.get("class", [])
    if isinstance(cur, str):
        cur = cur.split()
    cur = [c for c in cur if c != "secv-num-ok"]
    span["class"] = cur + ["secv-num-review"]
    span["title"] = title


def _is_review_figure(text: str) -> bool:
    """True for a real financial figure worth eyeballing — a significant
    amount, a currency figure, or a percentage.  Dates, years, note-reference
    numbers ("2.12") and small ordinals are excluded so the overlay marks
    line-item money, not furniture."""
    t = text.strip()
    if _NOTE_REF_RE.match(t):
        return False
    if "%" in t and re.search(r"\d", t):
        return True
    if re.search(r"[₹$]", t) and re.search(r"\d", t):
        return True
    for _s, _e, tok, key in iter_tokens(t):
        if is_significant(key, tok):
            return True
    return False

_XREF_RE = re.compile(
    r"\b(?:refer(?:\s+to)?\s+note|see\s+note|refer\s+note|note\s+no\.?|"
    r"as\s+per\s+note|schedule|annexure)\b[\s.]*\d{0,3}[A-Za-z.\d]*",
    re.I,
)
#: a unit word right after a number makes it a material figure even when the
#: digits are small — a scale word ("8 crore" = ₹80,000,000), a spelled-out
#: percentage ("51 per cent"), a per-share amount ("5 per share") or basis
#: points. Durations ("years"/"months") and counts ("times") are excluded.
_UNIT_RE = re.compile(
    r"^[\s ]*(?:crore|crores|lakh|lakhs|million|billion|thousand|mn|bn|per\s*cent|percent|percentage|basis\s+points|bps|per\s+share|per\s+equity\s+share)\b",
    re.I,
)


def _followed_by_scale(span) -> bool:
    """True when the text after *span* begins with a money/ratio unit word."""
    text, node = "", span.next_sibling
    while node is not None and len(text) < 16:
        text += node.get_text() if hasattr(node, "get_text") else str(node)
        node = node.next_sibling
    return bool(_UNIT_RE.match(text))


def _mark_prose_figures(soup, root) -> int:
    """Re-flag every validated (green) figure that is NOT inside a table as a
    blue manual-review figure.  Returns the count marked."""
    n = 0
    for span in root.find_all("span", class_="secv-num-ok"):
        if span.find_parent("table") is not None:
            continue  # in-table figures are positionally checked already
        if not (_is_review_figure(span.get_text()) or _followed_by_scale(span)):
            continue  # a date/year/note-ref, not a line-item money figure
        _paint_blue(
            span,
            "Manual-review zone — this figure is validated as present in the "
            "PDF, but it sits in prose, so its placement is not position-checked. "
            "Confirm by eye that it is against the right item.",
        )
        n += 1
    return n


def _mark_cross_references(soup, root) -> int:
    """Wrap every cross-reference phrase in a blue manual-review span."""
    n = 0
    for text_node in list(root.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        if text_node.find_parent(["script", "style"]) is not None:
            continue
        if text_node.find_parent(class_="secv-xref-review") is not None:
            continue
        s = str(text_node)
        if not _XREF_RE.search(s):
            continue
        parts = []
        last = 0
        for m in _XREF_RE.finditer(s):
            if m.start() > last:
                parts.append(soup.new_string(s[last:m.start()]))
            span = soup.new_tag("span", **{"class": "secv-xref-review"})
            span.string = m.group(0)
            span["title"] = (
                "Manual-review zone — verify this note/schedule reference points "
                "to the correct place; the tool cannot confirm cross-references."
            )
            parts.append(span)
            last = m.end()
            n += 1
        if last < len(s):
            parts.append(soup.new_string(s[last:]))
        text_node.replace_with(*parts)
    return n


def _clean_cell_text(cell) -> str:
    """Cell text with our own ``[n]`` markers removed, so the figure/label
    parse is not polluted by injected summary references."""
    return "".join(
        str(s)
        for s in cell.descendants
        if isinstance(s, NavigableString)
        and s.find_parent(class_="secv-marker") is None
    )


def _pdf_confirmer(corpus):
    """Return ``confirmed(label, figs)`` — True when some PDF row carries the
    same label (exact or close) with the same ordered figures."""
    from difflib import SequenceMatcher

    by_label: dict[str, set[tuple]] = {}
    for raw in corpus.pages_raw:
        for line in raw.splitlines():
            if not re.search(r"[A-Za-z]{3,}", line) or not re.search(r"\d", line):
                continue
            lbl, figs = line_label_figs(line)
            if lbl and figs:
                by_label.setdefault(lbl, set()).add(tuple(figs))

    def confirmed(lbl: str, figs: tuple) -> bool:
        if figs in by_label.get(lbl, ()):
            return True
        for plbl, pf in by_label.items():
            if (
                abs(len(plbl) - len(lbl)) <= 3
                and figs in pf
                and SequenceMatcher(None, lbl, plbl).ratio() >= 0.95
            ):
                return True
        return False

    return confirmed


def _parse_annotated_row(tr):
    """``(canonical label, figure keys, [green figure spans], cell count)`` for
    a table row in the *annotated* soup, ignoring injected ``[n]`` markers."""
    cells = tr.find_all(["td", "th"], recursive=False)
    cell_texts = [_clean_cell_text(cell) for cell in cells]
    lbl, figs = cells_label_figs(cell_texts)
    spans = []
    for cell in cells:
        spans.extend(cell.find_all("span", class_="secv-num-ok"))
    return lbl, figs, spans, len(cells)


def _mark_unconfirmed_table_rows(root, corpus) -> int:
    """Paint blue the figures of any real comparative line-item row (2–3
    figures, line-item label) whose exact ``(label, figures)`` the PDF does not
    confirm — the in-table hiding place for a value swapped between items."""
    if corpus is None or not getattr(corpus, "pages_raw", None):
        return 0
    confirmed = _pdf_confirmer(corpus)
    n = 0
    for tr in root.find_all("tr"):
        if tr.find_parent(class_="secv-callout") is not None:
            continue
        lbl, figs, fig_spans, ncells = _parse_annotated_row(tr)
        if ncells < 2 or len(lbl) < 10 or _FURNITURE_RE.search(lbl):
            continue
        if len(figs) not in (2, 3) or confirmed(lbl, tuple(figs)):
            continue
        for span in fig_spans:
            _paint_blue(
                span,
                "Manual-review zone — this row's exact figures could not be "
                "confirmed against the PDF (its label/values were not matched as "
                "a whole row). Verify the figures are against the right line item.",
            )
            n += 1
    return n


def _pdf_sequence(corpus):
    """``(positions, counts)`` for PDF rows in document order — ``positions``
    maps a label to ``[(seq_index, figure_tuple)]``; ``counts`` is a label
    frequency map used to find unique anchors and ambiguous repeats."""
    from collections import Counter

    seq = []
    for raw in corpus.pages_raw:
        for line in raw.splitlines():
            if not re.search(r"[A-Za-z]{3,}", line) or not re.search(r"\d", line):
                continue
            lbl, figs = line_label_figs(line)
            if lbl and figs:
                seq.append((lbl, tuple(figs)))
    positions: dict[str, list] = {}
    counts: Counter = Counter()
    for i, (l, f) in enumerate(seq):
        positions.setdefault(l, []).append((i, f))
        counts[l] += 1
    return positions, counts


def _mark_context_mismatched_rows(root, corpus) -> int:
    """Blue-mark a *repeated*-label row whose value disagrees with the PDF
    occurrence that shares its surroundings.

    A label that recurs (a line item's current and non-current portions, a
    "total" in several schedules) is disambiguated by its neighbours: each HTML
    statement table is pinned to a PDF region by the rows whose labels are
    unique on both sides, and a repeated-label row is then compared to the
    single PDF occurrence of that label inside that region.  This catches an
    *exchange* swap — two occurrences trading values — which every whole-row
    check passes because both values still exist against the label.  It is
    surfaced as blue (verify), never red: the region is inferred, so on an
    unusual layout an occasional mark is only a prompt to look.
    """
    if corpus is None or not getattr(corpus, "pages_raw", None):
        return 0
    from collections import Counter

    pdf_pos, pdf_count = _pdf_sequence(corpus)

    tables = []
    for table in root.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            if tr.find_parent(class_="secv-callout") is not None:
                continue
            lbl, figs, spans, ncells = _parse_annotated_row(tr)
            if ncells >= 2:
                rows.append((lbl, figs, spans))
        if sum(1 for l, f, s in rows if f and len(l) >= 4) >= 5:
            tables.append(rows)

    hcount = Counter(l for rows in tables for l, f, s in rows if l and f)

    def is_repeated_line_item(lbl, figs) -> bool:
        return bool(
            lbl and figs
            and len(lbl) >= 10
            and not _FURNITURE_RE.search(lbl)
            and len(figs) in (2, 3)
            and pdf_count.get(lbl, 0) >= 2      # a genuinely repeated label
        )

    n = 0
    for rows in tables:
        anchors = [
            l for l, f, s in rows
            if l and hcount[l] == 1 and pdf_count.get(l, 0) == 1 and len(l) >= 10
        ]
        if not anchors:
            # No neighbour is distinctive enough to pin this table to a PDF
            # region, so a repeated label here cannot be disambiguated at all —
            # mark those rows for the eye rather than leave them silent-green.
            for lbl, figs, spans in rows:
                if not is_repeated_line_item(lbl, figs):
                    continue
                for span in spans:
                    _paint_blue(
                        span,
                        "Manual-review zone — repeated label with no distinctive "
                        "neighbour to pin which occurrence this is (e.g. mirrored "
                        "segment/hierarchy tables). The tool cannot confirm the "
                        "value belongs here; verify against the PDF.",
                    )
                    n += 1
            continue
        apos = sorted(pdf_pos[l][0][0] for l in anchors)
        lo, hi = apos[0] - 8, apos[-1] + 8
        for lbl, figs, spans in rows:
            if not is_repeated_line_item(lbl, figs):
                continue
            in_window = [pf for (idx, pf) in pdf_pos[lbl] if lo <= idx <= hi]
            if len(in_window) != 1:               # can't disambiguate → skip
                continue
            if in_window[0] == tuple(figs):        # matches its context → correct
                continue
            for span in spans:
                _paint_blue(
                    span,
                    "Manual-review zone — repeated label. In this table's "
                    f"context the PDF shows “{' '.join(in_window[0])}” here, but "
                    f"the HTML shows “{' '.join(figs)}”. Two same-named rows may "
                    "have had their values swapped; verify which is correct.",
                )
                n += 1
    return n


def mark_review_zones(soup, root, corpus=None) -> tuple[int, int, int, int]:
    """Apply the blue overlay.

    Returns ``(prose_figures, cross_references, unconfirmed_table_figures,
    context_mismatched_figures)``.
    """
    intable = _mark_unconfirmed_table_rows(root, corpus)
    context = _mark_context_mismatched_rows(root, corpus)
    figs = _mark_prose_figures(soup, root)
    xrefs = _mark_cross_references(soup, root)
    return figs, xrefs, intable, context
