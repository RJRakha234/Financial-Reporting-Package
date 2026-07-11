#!/usr/bin/env python3
"""
common_notes_report.py
======================

Compare the *highlighted common notes* across a set of financial statements and
generate a self-contained HTML report with one column per statement.

A "common note" is a passage that the reviewer has HIGHLIGHTED in a PDF and to
which they have assigned a SERIAL NUMBER inside the highlight's comment/popup
box (e.g. "1.", "2."). A note is treated as *common* when the same serial number
is present in every document in the set.

The report answers one question at a glance: for each common note, is it
highlighted in all four statements and does it carry a consistent serial number
and consistent text?

Usage
-----
    python3 common_notes_report.py \
        --doc "IFRS USD Earnings Release=/path/a.pdf" \
        --doc "IFRS INR Consolidated=/path/b.pdf" \
        --doc "IGAAP Consolidated=/path/c.pdf" \
        --doc "Standalone=/path/d.pdf" \
        --out report.html

If no --doc arguments are given, the built-in DEFAULT_DOCS set is used.

Requires: PyMuPDF (pip install pymupdf)
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from typing import Optional

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    sys.exit("PyMuPDF is required. Install with:  pip install pymupdf")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

_UPLOAD_DIR = "/root/.claude/uploads/2527513b-fd2d-5b8f-9457-d0835a71e71e"

# Order here is the left-to-right column order in the report.
DEFAULT_DOCS: list[tuple[str, str]] = [
    ("IFRS · USD Earnings Release", os.path.join(_UPLOAD_DIR, "8b1385f0-ifrsusdearningsrelease_q1.pdf")),
    ("IFRS · INR Consolidated",     os.path.join(_UPLOAD_DIR, "ef6fc40d-consolifrsinrfy26q1finstatement.pdf")),
    ("IGAAP · Consolidated",        os.path.join(_UPLOAD_DIR, "0d0a84c3-consolfy26q1finstatement.pdf")),
    ("IGAAP · Standalone",          os.path.join(_UPLOAD_DIR, "81f78afd-safy26q1finstatement.pdf")),
]

# Highlight annotation subtypes we treat as a "mark".
_MARK_TYPES = {"Highlight", "Underline", "Squiggly", "StrikeOut"}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Mark:
    """A single highlighted passage in one document."""
    doc: str
    page: int
    serial_raw: str          # exactly what the reviewer typed in the comment box
    serial_key: Optional[str]  # normalized key ("1." -> "1"), None if no comment
    text: str                # the highlighted text
    heading: str             # section/heading context captured just above the mark


@dataclass
class NoteRow:
    """One common-note row: the same serial across (ideally) every document."""
    serial_key: str
    label: str                                    # e.g. "Note 1"
    section: str                                  # best human-readable section title
    cells: dict[str, Optional[Mark]] = field(default_factory=dict)  # doc -> mark or None

    @property
    def present_count(self) -> int:
        return sum(1 for m in self.cells.values() if m is not None)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _normalize_serial(raw: str) -> Optional[str]:
    """'1.' -> '1', 'Note 2' -> '2', '' -> None."""
    if not raw:
        return None
    m = re.search(r"\d+(?:\.\d+)?", raw)
    if not m:
        return _clean(raw).lower() or None
    return m.group(0).rstrip(".")


def _highlighted_text(page, annot) -> str:
    """Return the text covered by a highlight using its quad rectangles."""
    rects: list[fitz.Rect] = []
    verts = annot.vertices
    if verts and len(verts) % 4 == 0:
        for i in range(0, len(verts), 4):
            quad = verts[i:i + 4]
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            rects.append(fitz.Rect(min(xs), min(ys), max(xs), max(ys)))
    if not rects:
        rects = [annot.rect]

    words = page.get_text("words")
    picked: list[tuple[float, float, str]] = []
    for r in rects:
        for w in words:
            wr = fitz.Rect(w[:4])
            if wr.intersects(r):
                # sort key: line (y) then x
                picked.append((round(w[1], 1), w[0], w[4]))
    picked.sort()
    return _clean(" ".join(w[2] for w in picked))


def _heading_above(page, annot) -> str:
    """Grab a short slice of text just above the highlight for section context."""
    r = annot.rect
    clip = fitz.Rect(r.x0 - 4, max(0, r.y0 - 26), r.x1 + 360, r.y0 + 2)
    return _clean(page.get_text("text", clip=clip))[:140]


def extract_marks(label: str, path: str) -> list[Mark]:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    doc = fitz.open(path)
    marks: list[Mark] = []
    try:
        for pno in range(len(doc)):
            page = doc[pno]
            for annot in list(page.annots() or []):
                if annot.type[1] not in _MARK_TYPES:
                    continue
                raw = _clean(annot.info.get("content", ""))
                marks.append(Mark(
                    doc=label,
                    page=pno + 1,
                    serial_raw=raw,
                    serial_key=_normalize_serial(raw),
                    text=_highlighted_text(page, annot),
                    heading=_heading_above(page, annot),
                ))
    finally:
        doc.close()
    return marks


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def _derive_section(marks: list[Mark]) -> str:
    """Pick the most descriptive heading among a note's marks."""
    headings = [m.heading for m in marks if m.heading]
    if not headings:
        return ""
    return max(headings, key=len)


def _serial_sort_key(key: str):
    parts = re.findall(r"\d+", key)
    return (0, [int(p) for p in parts]) if parts else (1, key)


def build_notes(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]]) -> list[NoteRow]:
    """Group serial-numbered marks into note rows keyed by serial number."""
    serials: dict[str, list[Mark]] = {}
    for label in doc_labels:
        for m in marks_by_doc[label]:
            if m.serial_key is None:
                continue
            serials.setdefault(m.serial_key, []).append(m)

    rows: list[NoteRow] = []
    for key in sorted(serials, key=_serial_sort_key):
        group = serials[key]
        row = NoteRow(
            serial_key=key,
            label=f"Note {key}",
            section=_derive_section(group),
        )
        by_doc: dict[str, list[Mark]] = {}
        for m in group:
            by_doc.setdefault(m.doc, []).append(m)
        for label in doc_labels:
            hits = by_doc.get(label, [])
            row.cells[label] = hits[0] if hits else None
        rows.append(row)
    return rows


def text_consistency(row: NoteRow) -> float:
    """Lowest pairwise similarity of highlighted text among present cells (0..1)."""
    texts = [m.text for m in row.cells.values() if m and m.text]
    if len(texts) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            worst = min(worst, SequenceMatcher(None, texts[i], texts[j]).ratio())
    return worst


def serial_format_consistent(row: NoteRow) -> bool:
    fmts = {m.serial_raw for m in row.cells.values() if m}
    return len(fmts) <= 1


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

_CSS = """
:root{
  --paper:#f7f8fa; --card:#ffffff; --ink:#1a2233; --muted:#5b6472; --faint:#8a929e;
  --line:#e4e7ec; --line-strong:#cfd4dc;
  --accent:#0f6e78; --accent-soft:#e5f1f2;
  --good:#1f7a4d; --good-soft:#e6f2ec;
  --warn:#9a6a00; --warn-soft:#f7efdc;
  --bad:#a63232;  --bad-soft:#f6e6e6;
  --hi:#fff3bf;
  --shadow:0 1px 2px rgba(16,24,40,.04),0 6px 20px rgba(16,24,40,.06);
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#0e1218; --card:#161b22; --ink:#e7ebf0; --muted:#9aa4b2; --faint:#6b7482;
    --line:#242b34; --line-strong:#323a45;
    --accent:#4fc3cf; --accent-soft:#12292c;
    --good:#5ec98c; --good-soft:#12271c;
    --warn:#e0b154; --warn-soft:#2b2410;
    --bad:#e28282;  --bad-soft:#2b1616;
    --hi:#5a4d17;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35);
  }
}
:root[data-theme="light"]{
  --paper:#f7f8fa; --card:#ffffff; --ink:#1a2233; --muted:#5b6472; --faint:#8a929e;
  --line:#e4e7ec; --line-strong:#cfd4dc;
  --accent:#0f6e78; --accent-soft:#e5f1f2;
  --good:#1f7a4d; --good-soft:#e6f2ec;
  --warn:#9a6a00; --warn-soft:#f7efdc;
  --bad:#a63232;  --bad-soft:#f6e6e6; --hi:#fff3bf;
  --shadow:0 1px 2px rgba(16,24,40,.04),0 6px 20px rgba(16,24,40,.06);
}
:root[data-theme="dark"]{
  --paper:#0e1218; --card:#161b22; --ink:#e7ebf0; --muted:#9aa4b2; --faint:#6b7482;
  --line:#242b34; --line-strong:#323a45;
  --accent:#4fc3cf; --accent-soft:#12292c;
  --good:#5ec98c; --good-soft:#12271c;
  --warn:#e0b154; --warn-soft:#2b2410;
  --bad:#e28282;  --bad-soft:#2b1616; --hi:#5a4d17;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35);
}

*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1400px;margin:0 auto;padding:40px 28px 72px}

/* Masthead */
.masthead{border-bottom:1px solid var(--line-strong);padding-bottom:22px;margin-bottom:26px}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:600}
h1{font-family:Georgia,"Times New Roman",serif;font-weight:600;font-size:30px;line-height:1.15;
  margin:.35em 0 .2em;text-wrap:balance;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14.5px;max-width:70ch}

/* KPI strip */
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:24px 0 30px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;box-shadow:var(--shadow)}
.kpi .n{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.kpi .l{font-size:12.5px;color:var(--muted);margin-top:2px}
.kpi.good .n{color:var(--good)} .kpi.warn .n{color:var(--warn)} .kpi.accent .n{color:var(--accent)}

/* Column legend header */
.colhead{display:grid;grid-template-columns:210px repeat(var(--cols),1fr);gap:12px;
  position:sticky;top:0;z-index:5;background:var(--paper);
  padding:12px 0 10px;border-bottom:1px solid var(--line-strong);margin-bottom:8px}
.colhead .rail{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);
  align-self:end;font-weight:600}
.colhead .doc{font-size:13px;font-weight:650;color:var(--ink);align-self:end;line-height:1.25}
.colhead .doc small{display:block;color:var(--muted);font-weight:500;font-size:11.5px}

/* Note band */
.note{display:grid;grid-template-columns:210px repeat(var(--cols),1fr);gap:12px;
  padding:16px 0;border-bottom:1px solid var(--line)}
.rail{position:relative}
.rail .serial{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:30px;
  padding:0 8px;border-radius:8px;background:var(--accent);color:#fff;font-weight:700;
  font-variant-numeric:tabular-nums;font-size:15px}
.rail .rlabel{font-weight:650;margin-top:8px;font-size:14px}
.rail .rsec{color:var(--muted);font-size:12px;margin-top:3px;line-height:1.35}
.rail .status{margin-top:10px}

/* Cells */
.cell{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 12px;
  display:flex;flex-direction:column;gap:8px;min-width:0}
.cell.missing{background:transparent;border-style:dashed;border-color:var(--line-strong);
  align-items:center;justify-content:center;color:var(--faint);font-size:12.5px;min-height:64px}
.cell .meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.pill.ser{background:var(--accent-soft);color:var(--accent)}
.pill.pg{background:transparent;border:1px solid var(--line-strong);color:var(--muted)}
.cell .body{font-size:12.5px;color:var(--ink);
  background:linear-gradient(transparent 62%,var(--hi) 62%);
  display:inline;line-height:1.55}
.cell .clip{max-height:8.4em;overflow:hidden;position:relative}

/* status chips */
.chip{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:600;
  padding:3px 9px;border-radius:999px}
.chip::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.9}
.chip.good{background:var(--good-soft);color:var(--good)}
.chip.warn{background:var(--warn-soft);color:var(--warn)}
.chip.bad{background:var(--bad-soft);color:var(--bad)}

/* secondary sections */
h2{font-family:Georgia,serif;font-size:19px;margin:40px 0 6px;font-weight:600}
.lead{color:var(--muted);font-size:13.5px;margin:0 0 14px;max-width:80ch}
table.aux{width:100%;border-collapse:collapse;font-size:12.5px;background:var(--card);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
table.aux th,table.aux td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
table.aux th{background:var(--accent-soft);color:var(--accent);font-weight:650;font-size:11.5px;
  letter-spacing:.03em;text-transform:uppercase}
table.aux tr:last-child td{border-bottom:none}
td.pg{font-variant-numeric:tabular-nums;color:var(--muted);white-space:nowrap}
.foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);color:var(--faint);font-size:12px}
.scroll{overflow-x:auto}
@media (max-width:900px){
  .kpis{grid-template-columns:repeat(2,1fr)}
  .colhead,.note{grid-template-columns:150px repeat(var(--cols),minmax(180px,1fr));min-width:760px}
  .board{overflow-x:auto}
}
"""


def _status_chip(row: NoteRow, total_docs: int) -> str:
    present = row.present_count
    txt_ok = text_consistency(row) >= 0.90
    fmt_ok = serial_format_consistent(row)
    if present == total_docs and txt_ok and fmt_ok:
        return '<span class="chip good">Consistent</span>'
    if present == total_docs and (not txt_ok or not fmt_ok):
        issues = []
        if not fmt_ok:
            issues.append("serial format")
        if not txt_ok:
            issues.append("text differs")
        return f'<span class="chip warn">Check {" &amp; ".join(issues)}</span>'
    return f'<span class="chip bad">In {present}/{total_docs} only</span>'


def _cell_html(mark: Optional[Mark]) -> str:
    if mark is None:
        return '<div class="cell missing">not highlighted</div>'
    body = html.escape(mark.text) if mark.text else "<em>highlight has no text layer</em>"
    ser = html.escape(mark.serial_raw) if mark.serial_raw else "—"
    return (
        '<div class="cell">'
        '<div class="meta">'
        f'<span class="pill ser">serial {ser}</span>'
        f'<span class="pill pg">p.{mark.page}</span>'
        '</div>'
        f'<div class="clip"><span class="body">{body}</span></div>'
        '</div>'
    )


def render_html(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]],
                notes: list[NoteRow]) -> str:
    total = len(doc_labels)
    common = [n for n in notes if n.present_count == total]
    partial = [n for n in notes if n.present_count < total]
    fully_consistent = sum(
        1 for n in common
        if text_consistency(n) >= 0.90 and serial_format_consistent(n)
    )
    unnumbered = {lbl: [m for m in marks_by_doc[lbl] if m.serial_key is None] for lbl in doc_labels}
    unnumbered_total = sum(len(v) for v in unnumbered.values())

    # ---- column header
    colhead = ['<div class="colhead"><div class="rail">Common note</div>']
    for lbl in doc_labels:
        main, _, tail = lbl.partition("·")
        sub = f"<small>{html.escape(tail.strip())}</small>" if tail else ""
        colhead.append(f'<div class="doc">{html.escape(main.strip())}{sub}</div>')
    colhead.append("</div>")

    # ---- note bands (common first)
    bands = []
    for n in common + partial:
        rail = (
            '<div class="rail">'
            f'<span class="serial">{html.escape(n.serial_key)}</span>'
            f'<div class="rlabel">{html.escape(n.label)}</div>'
            f'<div class="rsec">{html.escape(n.section)}</div>'
            f'<div class="status">{_status_chip(n, total)}</div>'
            '</div>'
        )
        cells = "".join(_cell_html(n.cells[lbl]) for lbl in doc_labels)
        bands.append(f'<div class="note">{rail}{cells}</div>')

    # ---- unnumbered highlights table
    aux_rows = []
    for lbl in doc_labels:
        for m in sorted(unnumbered[lbl], key=lambda x: x.page):
            snippet = html.escape((m.heading or m.text)[:120])
            aux_rows.append(
                f"<tr><td>{html.escape(lbl)}</td><td class='pg'>p.{m.page}</td><td>{snippet}</td></tr>"
            )
    aux_table = (
        '<div class="scroll"><table class="aux"><thead><tr>'
        '<th>Document</th><th>Page</th><th>Highlighted passage (no serial assigned)</th>'
        '</tr></thead><tbody>' + ("".join(aux_rows) or
        "<tr><td colspan='3'>None — every highlight carries a serial number.</td></tr>") +
        '</tbody></table></div>'
    )

    gen = date.today().isoformat()
    doc_list = " · ".join(html.escape(l) for l in doc_labels)

    return f"""<div class="wrap" style="--cols:{total}">
  <header class="masthead">
    <div class="eyebrow">Financial Reporting Package · Note Reconciliation</div>
    <h1>Common Notes — Highlight &amp; Serial Consistency</h1>
    <p class="sub">Each highlighted note is matched across the four statements by the serial
    number entered in its comment box. A note is <strong>common</strong> when the same serial
    appears in every statement; the badge flags whether the serial format and highlighted text
    also agree.</p>
  </header>

  <section class="kpis">
    <div class="kpi accent"><div class="n">{total}</div><div class="l">Statements compared</div></div>
    <div class="kpi accent"><div class="n">{len(common)}</div><div class="l">Common notes (in all {total})</div></div>
    <div class="kpi good"><div class="n">{fully_consistent}</div><div class="l">Fully consistent</div></div>
    <div class="kpi warn"><div class="n">{unnumbered_total}</div><div class="l">Highlights without a serial</div></div>
  </section>

  <div class="board">
    {''.join(colhead)}
    {''.join(bands)}
  </div>

  <h2>Highlights not yet serial-numbered</h2>
  <p class="lead">These passages are highlighted but have no serial in the comment box, so they
  cannot be matched as common notes. They appear in only one statement in this set.</p>
  {aux_table}

  <footer class="foot">
    Generated {gen} · Documents: {doc_list} · Source of truth: PDF highlight annotations &amp; their comment text.
  </footer>
</div>"""


# --------------------------------------------------------------------------- #
# Excel rendering
# --------------------------------------------------------------------------- #

def render_xlsx(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]],
                notes: list[NoteRow], out_path: str) -> None:
    """Write a formatted workbook: common-notes matrix + unnumbered highlights."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    total = len(doc_labels)
    common = [n for n in notes if n.present_count == total]
    partial = [n for n in notes if n.present_count < total]

    # palette (aRGB, no leading '#')
    INK, ACCENT, ACC_SOFT = "1A2233", "0F6E78", "E5F1F2"
    GOOD, GOOD_SOFT = "1F7A4D", "E6F2EC"
    WARN, WARN_SOFT = "9A6A00", "F7EFDC"
    BAD, BAD_SOFT = "A63232", "F6E6E6"
    HI, ZEBRA, LINE = "FFF9E0", "F4F6F8", "D0D5DD"

    thin = Side(style="thin", color=LINE)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap_top = Alignment(wrap_text=True, vertical="top")
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    wb = Workbook()

    # ---------- Sheet 1: Common Notes ----------
    ws = wb.active
    ws.title = "Common Notes"
    ws.sheet_view.showGridLines = False

    headers = ["Serial", "Section"] + doc_labels + ["Status"]
    ncol = len(headers)

    # Title banner
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    t = ws.cell(1, 1, "Common Notes — Highlight & Serial Consistency")
    t.font = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor=ACCENT)
    t.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    s = ws.cell(2, 1, f"{total} statements · {len(common)} common notes · matched by comment-box serial number "
                      f"· generated {date.today().isoformat()}")
    s.font = Font(size=9, italic=True, color="5B6472")
    s.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 16

    # Header row
    hr = 3
    for c, name in enumerate(headers, start=1):
        cell = ws.cell(hr, c, name)
        cell.font = Font(bold=True, color=ACCENT, size=10)
        cell.fill = PatternFill("solid", fgColor=ACC_SOFT)
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[hr].height = 34

    def status_text(n: NoteRow) -> tuple[str, str, str]:
        present = n.present_count
        txt_ok = text_consistency(n) >= 0.90
        fmt_ok = serial_format_consistent(n)
        if present == total and txt_ok and fmt_ok:
            return "Consistent", GOOD, GOOD_SOFT
        if present == total:
            issues = []
            if not fmt_ok: issues.append("serial format")
            if not txt_ok: issues.append("text differs")
            return "Check " + " & ".join(issues), WARN, WARN_SOFT
        return f"In {present}/{total} only", BAD, BAD_SOFT

    r = hr + 1
    for i, n in enumerate(common + partial):
        zebra = ZEBRA if i % 2 else "FFFFFF"
        ws.cell(r, 1, n.serial_key).font = Font(bold=True, color=ACCENT, size=12)
        ws.cell(r, 1).alignment = center
        ws.cell(r, 2, n.section).alignment = wrap_top
        for c, lbl in enumerate(doc_labels, start=3):
            m = n.cells[lbl]
            if m is None:
                cell = ws.cell(r, c, "— not highlighted —")
                cell.font = Font(italic=True, color="8A929E", size=9)
            else:
                cell = ws.cell(r, c, f"[serial {m.serial_raw or '—'} · p.{m.page}]\n{m.text}")
                cell.font = Font(size=9)
                cell.fill = PatternFill("solid", fgColor=HI)
            cell.alignment = wrap_top
        stxt, scol, sfill = status_text(n)
        sc = ws.cell(r, ncol, stxt)
        sc.font = Font(bold=True, color=scol, size=9)
        sc.fill = PatternFill("solid", fgColor=sfill)
        sc.alignment = center
        # zebra + borders for non-highlighted cells
        for c in range(1, ncol + 1):
            cell = ws.cell(r, c)
            cell.border = border
            if cell.fill.fgColor.rgb in (None, "00000000"):
                cell.fill = PatternFill("solid", fgColor=zebra)
        ws.row_dimensions[r].height = 92
        r += 1

    # widths
    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 30
    for c in range(3, 3 + total):
        ws.column_dimensions[get_column_letter(c)].width = 40
    ws.column_dimensions[get_column_letter(ncol)].width = 16
    ws.freeze_panes = "C4"

    # ---------- Sheet 2: Unnumbered highlights ----------
    ws2 = wb.create_sheet("Unnumbered Highlights")
    ws2.sheet_view.showGridLines = False
    h2 = ["Document", "Page", "Highlighted passage (no serial assigned)"]
    for c, name in enumerate(h2, start=1):
        cell = ws2.cell(1, c, name)
        cell.font = Font(bold=True, color=ACCENT, size=10)
        cell.fill = PatternFill("solid", fgColor=ACC_SOFT)
        cell.alignment = center
        cell.border = border
    ws2.row_dimensions[1].height = 26
    rr = 2
    any_un = False
    for lbl in doc_labels:
        for m in sorted((x for x in marks_by_doc[lbl] if x.serial_key is None), key=lambda x: x.page):
            any_un = True
            ws2.cell(rr, 1, lbl).alignment = wrap_top
            ws2.cell(rr, 2, m.page).alignment = Alignment(horizontal="center", vertical="top")
            ws2.cell(rr, 3, m.heading or m.text).alignment = wrap_top
            for c in range(1, 4):
                cell = ws2.cell(rr, c)
                cell.border = border
                cell.font = Font(size=9)
                if rr % 2:
                    cell.fill = PatternFill("solid", fgColor=ZEBRA)
            ws2.row_dimensions[rr].height = 30
            rr += 1
    if not any_un:
        ws2.cell(2, 1, "None — every highlight carries a serial number.").font = Font(italic=True, color="5B6472")
    ws2.column_dimensions["A"].width = 30
    ws2.column_dimensions["B"].width = 8
    ws2.column_dimensions["C"].width = 90
    ws2.freeze_panes = "A2"

    wb.save(out_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_docs(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for p in pairs:
        if "=" not in p:
            sys.exit(f"--doc must be 'Label=/path.pdf', got: {p}")
        label, path = p.split("=", 1)
        out.append((label.strip(), os.path.expanduser(path.strip())))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare highlighted common notes across statements.")
    ap.add_argument("--doc", action="append", default=[],
                    help="Repeatable. Format: 'Label=/path/to.pdf' (column order preserved).")
    ap.add_argument("--out", default="common_notes_report.html", help="Output HTML file.")
    ap.add_argument("--xlsx", default=None,
                    help="Also write an Excel workbook to this path (e.g. report.xlsx).")
    args = ap.parse_args(argv)

    docs = parse_docs(args.doc) if args.doc else DEFAULT_DOCS
    labels = [d[0] for d in docs]

    marks_by_doc: dict[str, list[Mark]] = {}
    for label, path in docs:
        marks = extract_marks(label, path)
        marks_by_doc[label] = marks
        numbered = sum(1 for m in marks if m.serial_key is not None)
        print(f"  {label:32s} {len(marks):3d} highlights  ({numbered} serial-numbered)")

    notes = build_notes(labels, marks_by_doc)
    common = [n for n in notes if n.present_count == len(labels)]
    print(f"\n  {len(notes)} distinct serials · {len(common)} common across all {len(labels)} statements")

    body = render_html(labels, marks_by_doc, notes)
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Common Notes — Consistency Report</title>"
        f"<style>{_CSS}</style></head><body>{body}</body></html>"
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"\n  HTML report written to: {os.path.abspath(args.out)}")

    if args.xlsx:
        render_xlsx(labels, marks_by_doc, notes, args.xlsx)
        print(f"  Excel report written to: {os.path.abspath(args.xlsx)}")


if __name__ == "__main__":
    main()
