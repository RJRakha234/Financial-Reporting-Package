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


_APP_CSS = """
.app-loading{color:var(--muted);padding:48px 0;font-size:14px}
.intro{color:var(--muted);font-size:14.5px;max-width:74ch;margin:.4em 0 0}

/* sticky command bar */
.toolbar{position:sticky;top:0;z-index:30;margin:22px 0 26px;padding:14px 16px;border-radius:14px;
  background:var(--card);border:1px solid var(--line);box-shadow:var(--shadow);
  display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.progress{flex:1;min-width:240px}
.progress .ptop{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:7px}
.progress .plabel{font-size:12.5px;color:var(--muted);font-weight:600;letter-spacing:.02em}
.progress .ppct{font-size:14px;font-weight:750;font-variant-numeric:tabular-nums;color:var(--accent)}
.track{height:9px;border-radius:999px;background:var(--line);overflow:hidden;position:relative}
.bar{height:100%;width:0;border-radius:999px;
  background:linear-gradient(90deg,var(--accent),var(--good));
  transition:width .6s cubic-bezier(.22,1,.36,1)}
.bar.done{background:linear-gradient(90deg,var(--good),var(--good))}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{font:inherit;font-size:12.5px;font-weight:600;cursor:pointer;border-radius:9px;
  padding:8px 13px;border:1px solid var(--line-strong);background:var(--card);color:var(--ink);
  transition:transform .12s ease,border-color .15s,background .15s,color .15s}
.btn:hover{transform:translateY(-1px);border-color:var(--accent)}
.btn:active{transform:translateY(0)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.ghost{background:transparent}
.btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

/* note cards */
.notecard{background:var(--card);border:1px solid var(--line);border-radius:16px;
  box-shadow:var(--shadow);margin-bottom:18px;overflow:hidden;
  border-left:4px solid var(--line-strong);transition:border-color .3s,opacity .3s,transform .3s}
.notecard[data-sev="good"]{border-left-color:var(--good)}
.notecard[data-sev="warn"]{border-left-color:var(--warn)}
.notecard[data-sev="bad"]{border-left-color:var(--bad)}
.notecard.resolved{border-left-color:var(--good)}
.notecard.hide{display:none}
.note-head{display:flex;align-items:flex-start;gap:14px;padding:16px 18px;flex-wrap:wrap;
  border-bottom:1px solid var(--line)}
.badge-serial{flex:none;display:inline-flex;align-items:center;justify-content:center;
  width:44px;height:44px;border-radius:12px;background:var(--accent);color:#fff;
  font-weight:800;font-size:18px;font-variant-numeric:tabular-nums;
  box-shadow:0 4px 12px color-mix(in srgb,var(--accent) 40%,transparent)}
.note-head .htext{flex:1;min-width:200px}
.note-head .hlabel{font-weight:750;font-size:15.5px;letter-spacing:-.01em}
.note-head .hsec{color:var(--muted);font-size:12.5px;margin-top:2px;line-height:1.4}
.note-head .hstat{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.statpill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;
  padding:5px 12px;border-radius:999px;white-space:nowrap;transition:background .3s,color .3s}
.statpill::before{content:"";width:8px;height:8px;border-radius:50%;background:currentColor}
.statpill.good{background:var(--good-soft);color:var(--good)}
.statpill.warn{background:var(--warn-soft);color:var(--warn)}
.statpill.bad{background:var(--bad-soft);color:var(--bad)}
.statpill.resolved{background:var(--good-soft);color:var(--good)}
.bulk{display:flex;gap:6px}
.bulk button{font:inherit;font-size:11.5px;font-weight:650;cursor:pointer;border-radius:7px;
  padding:5px 10px;border:1px solid var(--line-strong);background:transparent;color:var(--muted);
  transition:all .13s}
.bulk button:hover{color:var(--ink);border-color:var(--accent)}

/* the four-financial grid */
.doc-grid{display:grid;grid-template-columns:repeat(var(--cols),minmax(0,1fr));gap:0}
.dcell{padding:15px 16px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:10px;
  min-width:0;transition:background .35s ease}
.dcell:last-child{border-right:none}
.dcell .dtop{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.dcell .dname{font-weight:700;font-size:12px;letter-spacing:.02em;color:var(--ink)}
.dcell .dname small{display:block;color:var(--muted);font-weight:500;font-size:11px}
.chiprow{display:flex;gap:6px;flex-wrap:wrap;margin-top:2px}
.mini{font-size:10.5px;font-weight:650;padding:2px 8px;border-radius:999px;
  font-variant-numeric:tabular-nums;white-space:nowrap;border:1px solid var(--line-strong);color:var(--muted)}
.mini.ser{background:var(--accent-soft);color:var(--accent);border-color:transparent}
.mini.warn{background:var(--warn-soft);color:var(--warn);border-color:transparent}
.mini.bad{background:var(--bad-soft);color:var(--bad);border-color:transparent}
.dtext{font-size:12px;line-height:1.55;color:var(--ink);
  background:linear-gradient(transparent 60%,var(--hi) 60%);display:inline}
.dclip{max-height:7.2em;overflow:hidden;position:relative;transition:max-height .3s ease}
.dclip.open{max-height:2000px}
.expand{align-self:flex-start;font:inherit;font-size:11px;font-weight:650;color:var(--accent);
  background:none;border:none;cursor:pointer;padding:0}
.tag-match{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:650;
  color:var(--good);margin-top:auto}
.tag-match::before{content:"✓";font-weight:800}

/* decision control */
.decision{margin-top:auto;display:flex;flex-direction:column;gap:8px}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.seg button{font:inherit;font-size:12px;font-weight:700;cursor:pointer;border-radius:9px;padding:9px 8px;
  border:1.5px solid var(--line-strong);background:var(--card);color:var(--muted);
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  transition:all .16s cubic-bezier(.22,1,.36,1)}
.seg button:hover{border-color:var(--accent);color:var(--ink);transform:translateY(-1px)}
.seg button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.seg .acc[aria-pressed="true"]{background:var(--good);border-color:var(--good);color:#fff;
  box-shadow:0 4px 14px color-mix(in srgb,var(--good) 35%,transparent)}
.seg .rej[aria-pressed="true"]{background:var(--bad);border-color:var(--bad);color:#fff;
  box-shadow:0 4px 14px color-mix(in srgb,var(--bad) 35%,transparent)}
.seg button .pop{display:inline-block;transform:scale(0);transition:transform .25s cubic-bezier(.34,1.56,.64,1)}
.seg button[aria-pressed="true"] .pop{transform:scale(1)}
.dcell[data-state="accepted"]{background:var(--good-soft)}
.dcell[data-state="rejected"]{background:var(--bad-soft)}
.dcell[data-state="rejected"] .dtext{background:none;text-decoration:line-through;
  text-decoration-color:var(--bad);opacity:.7}
.difflink{font:inherit;font-size:11px;font-weight:650;color:var(--accent);background:none;border:none;
  cursor:pointer;padding:0;align-self:flex-start}
.diffbox{font-size:11.5px;line-height:1.6;background:var(--paper);border:1px dashed var(--line-strong);
  border-radius:9px;padding:9px 10px;display:none}
.diffbox.open{display:block}
.diffbox .add{background:var(--good-soft);color:var(--good);border-radius:3px;padding:0 2px;font-weight:600}
.diffbox .del{background:var(--bad-soft);color:var(--bad);border-radius:3px;padding:0 2px;
  text-decoration:line-through}
.diffbox .dh{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
  font-weight:700;margin-bottom:5px}
.reasons{display:flex;gap:6px;flex-wrap:wrap}

/* confetti + celebrate banner */
#confetti{position:fixed;inset:0;pointer-events:none;z-index:60}
.celebrate{margin:0 0 18px;padding:14px 18px;border-radius:14px;display:none;align-items:center;gap:12px;
  background:var(--good-soft);border:1px solid color-mix(in srgb,var(--good) 40%,var(--line));
  color:var(--good);font-weight:650;font-size:14px}
.celebrate.show{display:flex;animation:pop .4s cubic-bezier(.34,1.56,.64,1)}
.celebrate .big{font-size:22px}
@keyframes pop{from{transform:scale(.9);opacity:0}to{transform:scale(1);opacity:1}}
.filterwrap{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);font-weight:600}
.switch{position:relative;width:38px;height:22px;border-radius:999px;background:var(--line-strong);
  cursor:pointer;transition:background .2s;flex:none}
.switch.on{background:var(--accent)}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;
  background:#fff;transition:transform .2s}
.switch.on::after{transform:translateX(16px)}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:820px){
  .doc-grid{grid-template-columns:1fr}
  .dcell{border-right:none;border-bottom:1px solid var(--line)}
}
"""

_APP_JS = r"""
(function(){
  var DATA = JSON.parse(document.getElementById('cn-data').textContent);
  var COLS = DATA.docs.length;
  var app = document.getElementById('app');

  // ---- persistence -------------------------------------------------------
  var SIG = 'cnrev:' + DATA.notes.map(function(n){return n.serial;}).join(',') +
            '|' + DATA.docs.map(function(d){return d.label;}).join(',');
  var store = {};
  try { store = JSON.parse(localStorage.getItem(SIG) || '{}') || {}; } catch(e){ store = {}; }
  function persist(){ try { localStorage.setItem(SIG, JSON.stringify(store)); } catch(e){} }
  function key(serial, doc){ return serial + '||' + doc; }
  function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  // ---- required decisions bookkeeping -----------------------------------
  function noteRequired(n){ return n.cells.filter(function(c){return c.differs;}); }
  function noteDecided(n){ return noteRequired(n).filter(function(c){return store[key(n.serial,c.doc)];}).length; }
  function totals(){
    var req=0, dec=0;
    DATA.notes.forEach(function(n){ var r=noteRequired(n); req+=r.length;
      dec+=r.filter(function(c){return store[key(n.serial,c.doc)];}).length; });
    return {req:req, dec:dec};
  }

  // ---- word-level diff (LCS) --------------------------------------------
  function tok(s){ return (s||'').split(/(\s+)/).filter(function(w){return w.length;}); }
  function wordDiff(ref, cur){
    var a=tok(ref), b=tok(cur), m=a.length, n=b.length;
    var dp=[]; for(var i=0;i<=m;i++){ dp.push(new Array(n+1).fill(0)); }
    for(var i=m-1;i>=0;i--) for(var j=n-1;j>=0;j--)
      dp[i][j] = a[i]===b[j] ? dp[i+1][j+1]+1 : Math.max(dp[i+1][j], dp[i][j+1]);
    var out=[], i=0, j=0;
    while(i<m && j<n){
      if(a[i]===b[j]){ out.push({op:'=',t:b[j]}); i++; j++; }
      else if(dp[i+1][j]>=dp[i][j+1]){ out.push({op:'-',t:a[i]}); i++; }
      else { out.push({op:'+',t:b[j]}); j++; }
    }
    while(i<m){ out.push({op:'-',t:a[i++]}); }
    while(j<n){ out.push({op:'+',t:b[j++]}); }
    return out;
  }
  function diffHTML(ref, cur){
    return wordDiff(ref,cur).map(function(p){
      if(p.op==='=') return esc(p.t);
      if(p.op==='+') return '<span class="add">'+esc(p.t)+'</span>';
      return '<span class="del">'+esc(p.t)+'</span>';
    }).join('');
  }

  // ---- rendering ---------------------------------------------------------
  function docMeta(label){ for(var i=0;i<DATA.docs.length;i++) if(DATA.docs[i].label===label) return DATA.docs[i]; return {main:label,sub:''}; }

  function render(){
    var k = DATA.kpi;
    var html = ''+
      '<header class="masthead">'+
      '<div class="eyebrow">Financial Reporting Package · Interactive Note Review</div>'+
      '<h1>Common Notes — Accept / Reject Console</h1>'+
      '<p class="intro">For every common note, each statement is compared against the reference version. '+
      'Where a financial <strong>differs</strong>, accept it (an acceptable variation) or reject it (needs correction). '+
      'Matching statements are marked automatically. Your decisions are saved in this browser and can be exported.</p>'+
      '</header>'+
      '<section class="kpis">'+
        kpi('accent', k.total, 'Statements') +
        kpi('accent', k.common, 'Common notes') +
        kpi('warn', k.needsReview, 'Notes needing review') +
        kpi('good', '<span id="kResolved">0</span>/'+k.needsReview, 'Notes resolved') +
      '</section>'+
      '<div class="celebrate" id="celebrate"><span class="big">🎉</span>'+
        '<span>All differences reviewed — every note is reconciled. Export your decisions to lock it in.</span></div>'+
      '<div class="toolbar">'+
        '<div class="progress"><div class="ptop"><span class="plabel">Decisions completed</span>'+
        '<span class="ppct" id="ppct">0%</span></div>'+
        '<div class="track"><div class="bar" id="bar"></div></div></div>'+
        '<div class="filterwrap"><span>Only unresolved</span><div class="switch" id="flt" role="switch" aria-checked="false" tabindex="0"></div></div>'+
        '<div class="actions">'+
          '<button class="btn ghost" id="expCsv">Export CSV</button>'+
          '<button class="btn ghost" id="expJson">Export JSON</button>'+
          '<button class="btn ghost" id="reset">Reset</button>'+
        '</div>'+
      '</div>'+
      '<div id="notes">' + DATA.notes.map(noteCard).join('') + '</div>'+
      '<canvas id="confetti"></canvas>';
    app.className = '';
    app.innerHTML = html;
    wire();
    refresh();
  }
  function kpi(cls,n,l){ return '<div class="kpi '+cls+'"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>'; }

  function noteCard(n){
    var cells = n.cells.map(function(c){ return cell(n,c); }).join('');
    var bulk = n.hasDiff ? '<div class="bulk">'+
        '<button data-bulk="accepted" data-serial="'+esc(n.serial)+'">Accept all</button>'+
        '<button data-bulk="rejected" data-serial="'+esc(n.serial)+'">Reject all</button></div>' : '';
    return '<article class="notecard" data-sev="'+n.severity+'" data-serial="'+esc(n.serial)+'" data-hasdiff="'+(n.hasDiff?1:0)+'">'+
      '<div class="note-head">'+
        '<span class="badge-serial">'+esc(n.serial)+'</span>'+
        '<div class="htext"><div class="hlabel">'+esc(n.label)+'</div>'+
          '<div class="hsec">'+esc(n.section)+'</div></div>'+
        '<div class="hstat"><span class="statpill" data-stat></span>'+bulk+'</div>'+
      '</div>'+
      '<div class="doc-grid" style="--cols:'+COLS+'">'+cells+'</div>'+
    '</article>';
  }

  function cell(n,c){
    var meta = docMeta(c.doc);
    var name = '<div class="dname">'+esc(meta.main)+(meta.sub?'<small>'+esc(meta.sub)+'</small>':'')+'</div>';
    var body, chips='', decision='';
    if(!c.present){
      chips = '<span class="mini bad">not highlighted</span>';
      body = '<div class="dtext" style="background:none;color:var(--faint);font-style:italic">This note is not highlighted in this statement.</div>';
    } else {
      var scls = c.reasons.indexOf('serial format')>=0 ? 'mini ser warn' : 'mini ser';
      chips = '<span class="'+scls+'">serial '+esc(c.serial||'—')+'</span>'+
              '<span class="mini">p.'+c.page+'</span>'+
              (c.reasons.indexOf('text differs')>=0 ? '<span class="mini bad">'+Math.round(c.sim*100)+'% match</span>' : '');
      body = '<div class="dclip"><span class="dtext">'+esc(c.text)+'</span></div>'+
             '<button class="expand" data-exp>Show full text ▾</button>';
    }
    if(c.differs){
      var diff = (c.present && n.refText) ?
        '<button class="difflink" data-diff>Compare to reference ▾</button>'+
        '<div class="diffbox"><div class="dh">Reference vs this statement — '+
        '<span class="add">added</span> / <span class="del">missing</span></div>'+diffHTML(n.refText, c.text)+'</div>' : '';
      decision = '<div class="decision">'+diff+
        '<div class="seg" role="group" aria-label="decision">'+
          '<button class="acc" data-dec="accepted" aria-pressed="false"><span class="pop">✓</span> Accept</button>'+
          '<button class="rej" data-dec="rejected" aria-pressed="false"><span class="pop">✕</span> Reject</button>'+
        '</div></div>';
    } else {
      decision = '<div class="tag-match">Matches reference</div>';
    }
    return '<div class="dcell" data-serial="'+esc(n.serial)+'" data-doc="'+esc(c.doc)+'" data-differs="'+(c.differs?1:0)+'">'+
      '<div class="dtop">'+name+'</div>'+
      '<div class="chiprow">'+chips+'</div>'+
      body + decision +
    '</div>';
  }

  // ---- interaction -------------------------------------------------------
  function setDecision(serial, doc, val){
    var k = key(serial,doc);
    if(store[k]===val){ delete store[k]; } else { store[k]=val; }
    persist();
    syncCell(serial,doc);
    refresh();
  }
  function syncCell(serial,doc){
    var cell = app.querySelector('.dcell[data-serial="'+cssesc(serial)+'"][data-doc="'+cssesc(doc)+'"]');
    if(!cell) return;
    var st = store[key(serial,doc)] || '';
    cell.setAttribute('data-state', st);
    var accB = cell.querySelector('.acc'), rejB = cell.querySelector('.rej');
    if(accB) accB.setAttribute('aria-pressed', st==='accepted');
    if(rejB) rejB.setAttribute('aria-pressed', st==='rejected');
  }
  function cssesc(s){ return String(s).replace(/["\\]/g,'\\$&'); }

  function refresh(){
    var doneNotes=0;
    DATA.notes.forEach(function(n){
      var card = app.querySelector('.notecard[data-serial="'+cssesc(n.serial)+'"]');
      var pill = card.querySelector('[data-stat]');
      if(!n.hasDiff){ pill.className='statpill good'; pill.textContent='Consistent'; return; }
      var req = noteRequired(n).length, dec = noteDecided(n);
      var rejd = noteRequired(n).filter(function(c){return store[key(n.serial,c.doc)]==='rejected';}).length;
      if(dec>=req){
        doneNotes++;
        card.classList.add('resolved');
        pill.className='statpill resolved';
        pill.textContent = rejd ? ('Resolved · '+rejd+' rejected') : 'Resolved · all accepted';
      } else {
        card.classList.remove('resolved');
        pill.className='statpill warn';
        pill.textContent = (req-dec)+' of '+req+' pending';
      }
    });
    var t=totals(), pct = t.req? Math.round(t.dec/t.req*100) : 100;
    var bar=document.getElementById('bar'); bar.style.width=pct+'%'; bar.classList.toggle('done',pct===100);
    document.getElementById('ppct').textContent = pct+'%';
    var kr=document.getElementById('kResolved'); if(kr) kr.textContent=doneNotes;
    applyFilter();
    celebrate(t.req>0 && t.dec>=t.req);
  }

  var filterOn=false;
  function applyFilter(){
    DATA.notes.forEach(function(n){
      var card=app.querySelector('.notecard[data-serial="'+cssesc(n.serial)+'"]');
      var resolved = !n.hasDiff || noteDecided(n)>=noteRequired(n).length;
      card.classList.toggle('hide', filterOn && resolved);
    });
  }

  var celebrated=false;
  function celebrate(done){
    var el=document.getElementById('celebrate');
    el.classList.toggle('show', done);
    if(done && !celebrated){ celebrated=true; burst(); }
    if(!done) celebrated=false;
  }

  // ---- confetti ----------------------------------------------------------
  function burst(){
    if(window.matchMedia && matchMedia('(prefers-reduced-motion:reduce)').matches) return;
    var cv=document.getElementById('confetti'), ctx=cv.getContext('2d');
    cv.width=innerWidth; cv.height=innerHeight;
    var cols=['#0f6e78','#1f7a4d','#4fc3cf','#e0b154','#a63232'], P=[];
    for(var i=0;i<140;i++) P.push({x:innerWidth/2,y:innerHeight*0.28,
      vx:(Math.random()-0.5)*11, vy:Math.random()*-13-4, g:0.32+Math.random()*0.12,
      s:5+Math.random()*6, c:cols[i%cols.length], r:Math.random()*6, vr:(Math.random()-.5)*.4});
    var t0=performance.now();
    (function frame(now){
      var e=now-t0; ctx.clearRect(0,0,cv.width,cv.height);
      P.forEach(function(p){ p.vy+=p.g; p.x+=p.vx; p.y+=p.vy; p.r+=p.vr;
        ctx.save(); ctx.translate(p.x,p.y); ctx.rotate(p.r);
        ctx.fillStyle=p.c; ctx.globalAlpha=Math.max(0,1-e/1600);
        ctx.fillRect(-p.s/2,-p.s/2,p.s,p.s*0.6); ctx.restore(); });
      if(e<1600) requestAnimationFrame(frame); else ctx.clearRect(0,0,cv.width,cv.height);
    })(t0);
  }

  // ---- events ------------------------------------------------------------
  function wire(){
    app.addEventListener('click', function(ev){
      var t=ev.target;
      var dec=t.closest('[data-dec]');
      if(dec){ var cell=dec.closest('.dcell');
        setDecision(cell.getAttribute('data-serial'), cell.getAttribute('data-doc'), dec.getAttribute('data-dec')); return; }
      var bulk=t.closest('[data-bulk]');
      if(bulk){ var s=bulk.getAttribute('data-serial'), v=bulk.getAttribute('data-bulk');
        var n=DATA.notes.find(function(x){return x.serial===s;});
        noteRequired(n).forEach(function(c){ store[key(s,c.doc)]=v; syncCell(s,c.doc); });
        persist(); refresh(); return; }
      if(t.closest('[data-exp]')){ var b=t.closest('[data-exp]'); var clip=b.previousElementSibling;
        clip.classList.toggle('open'); b.textContent = clip.classList.contains('open')?'Show less ▴':'Show full text ▾'; return; }
      if(t.closest('[data-diff]')){ var d=t.closest('[data-diff]'); d.nextElementSibling.classList.toggle('open');
        d.textContent = d.nextElementSibling.classList.contains('open')?'Hide comparison ▴':'Compare to reference ▾'; return; }
      if(t.id==='expCsv'){ exportCsv(); return; }
      if(t.id==='expJson'){ exportJson(); return; }
      if(t.id==='reset'){ if(confirm('Clear all accept/reject decisions?')){ store={}; persist();
        app.querySelectorAll('.dcell').forEach(function(c){ c.removeAttribute('data-state');
          var a=c.querySelector('.acc'),r=c.querySelector('.rej'); if(a)a.setAttribute('aria-pressed',false); if(r)r.setAttribute('aria-pressed',false); });
        celebrated=false; refresh(); } return; }
      if(t.id==='flt' || t.closest('#flt')){ filterOn=!filterOn;
        var sw=document.getElementById('flt'); sw.classList.toggle('on',filterOn); sw.setAttribute('aria-checked',filterOn); applyFilter(); return; }
    });
    app.addEventListener('keydown', function(ev){
      if((ev.target.id==='flt') && (ev.key==='Enter'||ev.key===' ')){ ev.preventDefault(); ev.target.click(); }
    });
    addEventListener('resize', function(){ var cv=document.getElementById('confetti'); if(cv){cv.width=innerWidth;cv.height=innerHeight;} });
  }

  // ---- export ------------------------------------------------------------
  function decisionOf(n,c){
    if(!c.differs) return 'auto-match';
    return store[key(n.serial,c.doc)] || 'pending';
  }
  function download(name, mime, text){
    var blob=new Blob([text],{type:mime}), url=URL.createObjectURL(blob);
    var a=document.createElement('a'); a.href=url; a.download=name; document.body.appendChild(a); a.click();
    setTimeout(function(){ URL.revokeObjectURL(url); a.remove(); }, 500);
  }
  function exportCsv(){
    var rows=[['serial','note','section','document','page','serial_in_doc','differs','reasons','match_pct','decision','text']];
    DATA.notes.forEach(function(n){ n.cells.forEach(function(c){
      rows.push([n.serial,n.label,n.section,c.doc,c.page==null?'':c.page,c.serial,c.differs?'yes':'no',
        c.reasons.join('; '), c.present?Math.round(c.sim*100)+'%':'', decisionOf(n,c), c.text]); }); });
    var csv=rows.map(function(r){ return r.map(function(v){
      v=(v==null?'':String(v)); return /[",\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; }).join(','); }).join('\r\n');
    download('common_notes_decisions.csv','text/csv;charset=utf-8', '﻿'+csv);
  }
  function exportJson(){
    var out={ generated:DATA.generated, exportedAt:new Date().toISOString(), documents:DATA.docs.map(function(d){return d.label;}), notes:[] };
    DATA.notes.forEach(function(n){ out.notes.push({ serial:n.serial, label:n.label, section:n.section,
      referenceSerial:n.refSerial, decisions:n.cells.map(function(c){ return {document:c.doc, page:c.page,
        differs:c.differs, reasons:c.reasons, matchPct:c.present?Math.round(c.sim*100):null, decision:decisionOf(n,c)}; }) }); });
    download('common_notes_decisions.json','application/json', JSON.stringify(out,null,2));
  }

  render();
})();
"""


def _note_status(row: NoteRow, total_docs: int) -> tuple[str, str, bool]:
    """Return (status_label, severity, has_differences) for a note."""
    present = row.present_count
    txt_ok = text_consistency(row) >= 0.985
    fmt_ok = serial_format_consistent(row)
    if present == total_docs and txt_ok and fmt_ok:
        return "Consistent", "good", False
    if present == total_docs:
        return "Needs review", "warn", True
    return f"In {present}/{total_docs} only", "bad", True


def build_payload(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]],
                  notes: list[NoteRow]) -> dict:
    """Assemble the JSON model the interactive front-end renders from."""
    from collections import Counter

    total = len(doc_labels)
    common = [n for n in notes if n.present_count == total]
    partial = [n for n in notes if n.present_count < total]

    docs_meta = []
    for lbl in doc_labels:
        main, _, tail = lbl.partition("·")
        docs_meta.append({"label": lbl, "main": main.strip(), "sub": tail.strip()})

    note_objs = []
    for n in common + partial:
        present_marks = [m for m in n.cells.values() if m]
        serials_raw = [m.serial_raw for m in present_marks if m.serial_raw]
        texts = [m.text for m in present_marks if m.text]
        ref_serial = Counter(serials_raw).most_common(1)[0][0] if serials_raw else ""
        ref_text = Counter(texts).most_common(1)[0][0] if texts else ""

        cells = []
        for lbl in doc_labels:
            m = n.cells[lbl]
            if m is None:
                cells.append({
                    "doc": lbl, "present": False, "page": None, "serial": "",
                    "text": "", "differs": True, "reasons": ["not highlighted"], "sim": 0.0,
                })
                continue
            reasons = []
            serial_diff = (m.serial_raw or "").strip() != (ref_serial or "").strip()
            sim = SequenceMatcher(None, m.text, ref_text).ratio() if ref_text else 1.0
            text_diff = sim < 0.985
            if serial_diff:
                reasons.append("serial format")
            if text_diff:
                reasons.append("text differs")
            cells.append({
                "doc": lbl, "present": True, "page": m.page, "serial": m.serial_raw,
                "text": m.text, "differs": bool(reasons), "reasons": reasons, "sim": round(sim, 3),
            })

        label, severity, has_diff = _note_status(n, total)
        note_objs.append({
            "serial": n.serial_key, "label": n.label, "section": n.section,
            "refSerial": ref_serial, "refText": ref_text,
            "status": label, "severity": severity, "hasDiff": has_diff,
            "diffCount": sum(1 for c in cells if c["differs"]),
            "cells": cells,
        })

    unnumbered = []
    for lbl in doc_labels:
        for m in sorted((x for x in marks_by_doc[lbl] if x.serial_key is None), key=lambda x: x.page):
            unnumbered.append({"doc": lbl, "page": m.page, "text": (m.heading or m.text)[:160]})

    fully_consistent = sum(1 for n in note_objs if not n["hasDiff"] and
                           all(c["present"] for c in n["cells"]))
    return {
        "generated": date.today().isoformat(),
        "docs": docs_meta,
        "notes": note_objs,
        "unnumbered": unnumbered,
        "kpi": {
            "total": total,
            "common": len(common),
            "consistent": fully_consistent,
            "needsReview": sum(1 for n in note_objs if n["hasDiff"]),
            "unnumbered": len(unnumbered),
        },
    }


def render_html(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]],
                notes: list[NoteRow]) -> str:
    payload = build_payload(doc_labels, marks_by_doc, notes)
    import json
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (
        f"<style>{_APP_CSS}</style>\n"
        '<div class="wrap"><div id="app" class="app-loading">Loading review board…</div></div>\n'
        f'<script id="cn-data" type="application/json">{data_json}</script>\n'
        f"<script>{_APP_JS}</script>"
    )


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
