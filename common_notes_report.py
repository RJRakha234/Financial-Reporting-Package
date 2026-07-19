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
    ("Ind AS · Consolidated",       os.path.join(_UPLOAD_DIR, "0d0a84c3-consolfy26q1finstatement.pdf")),
    ("Ind AS · Standalone",         os.path.join(_UPLOAD_DIR, "81f78afd-safy26q1finstatement.pdf")),
    ("IFRS · INR Consolidated",     os.path.join(_UPLOAD_DIR, "ef6fc40d-consolifrsinrfy26q1finstatement.pdf")),
    ("IFRS · USD Earnings Release", os.path.join(_UPLOAD_DIR, "8b1385f0-ifrsusdearningsrelease_q1.pdf")),
]

# The statement every other financial is benchmarked against. If None, the tool
# auto-picks the label matching both "Ind AS"/"Ind" and "Consol", else the first doc.
DEFAULT_BENCHMARK = "Ind AS · Consolidated"


def resolve_benchmark(doc_labels: list[str], requested: Optional[str]) -> str:
    """Pick the primary benchmark document label from the set."""
    if requested and requested in doc_labels:
        return requested
    for lbl in doc_labels:  # auto-detect a consolidated Ind AS statement
        low = lbl.lower()
        if "consol" in low and ("ind as" in low or "indas" in low or "ind-as" in low):
            return lbl
    return doc_labels[0]


def resolve_fallback_benchmark(doc_labels: list[str], primary: str) -> Optional[str]:
    """Secondary benchmark used for a note the primary statement does not highlight.

    Defaults to the IFRS INR Consolidated statement (a consolidated basis like the
    primary), so a note absent from Ind AS Consolidated is still benchmarked against
    a consolidated financial rather than a standalone one.
    """
    for lbl in doc_labels:
        low = lbl.lower()
        if lbl != primary and "ifrs" in low and "inr" in low:
            return lbl
    for lbl in doc_labels:  # any other consolidated statement
        if lbl != primary and "consol" in lbl.lower():
            return lbl
    return None

# Highlight annotation subtypes we treat as a "mark".
_MARK_TYPES = {"Highlight", "Underline", "Squiggly", "StrikeOut"}

# Author name the tool writes on its own highlight annotations.
MACHINE_AUTHOR = "Common Notes Tool"


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
    y0: float = 0.0          # top of the highlight on the page (for reading order)
    x0: float = 0.0          # left edge (tie-break within a line)
    conf: Optional[float] = None  # set when auto-located from a template (0..1)
    author: str = ""         # annotation author; the tool signs its own highlights


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
        sys.exit(f"error: file not found for '{label}': {path}")
    try:
        doc = fitz.open(path)
        if doc.needs_pass:
            sys.exit(f"error: '{label}' is password-protected: {path}")
        _ = len(doc)
    except SystemExit:
        raise
    except Exception as exc:
        sys.exit(f"error: cannot open '{label}' ({path}): {exc}")
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
                    y0=annot.rect.y0,
                    x0=annot.rect.x0,
                    author=_clean(annot.info.get("title", "")),
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


def _merge_marks(hits: list[Mark]) -> Mark:
    """Merge multiple same-serial highlights in one document into one Mark.

    Reviewers sometimes highlight a note as several strokes (e.g. the heading
    plus the paragraph) sharing one serial. Joining them in reading order
    (page, then top-to-bottom, then left-to-right) makes the comparison see
    the same combined passage in every document instead of an arbitrary piece.
    """
    if len(hits) == 1:
        return hits[0]
    ordered = sorted(hits, key=lambda m: (m.page, m.y0, m.x0))
    first = ordered[0]
    return Mark(
        doc=first.doc,
        page=first.page,
        serial_raw=first.serial_raw,
        serial_key=first.serial_key,
        text=_clean(" ".join(m.text for m in ordered if m.text)),
        heading=max((m.heading for m in ordered), key=len, default=""),
        y0=first.y0,
        x0=first.x0,
    )


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
            # A reviewer's own highlight overrides the tool's machine-drawn one
            # for the same serial (correction workflow: re-highlight, re-run).
            manual = [m for m in hits if m.author != MACHINE_AUTHOR]
            if manual and len(manual) < len(hits):
                hits = manual
            row.cells[label] = _merge_marks(hits) if hits else None
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Template: learn common notes once, locate them in unhighlighted periods
# --------------------------------------------------------------------------- #

def save_template(path: str, doc_labels: list[str], notes: list[NoteRow],
                  benchmark: Optional[str]) -> None:
    """Persist the highlighted common-note passages as a reusable template."""
    import json
    bench = resolve_benchmark(doc_labels, benchmark)
    tpl = {
        "version": 1,
        "created": date.today().isoformat(),
        "benchmark": bench,
        "docs": doc_labels,
        "notes": [],
    }
    for n in notes:
        entry = {"serial": n.serial_key, "section": n.section, "passages": {}}
        for lbl, m in n.cells.items():
            if m:
                entry["passages"][lbl] = {
                    "text": m.text, "serial_raw": m.serial_raw, "page": m.page,
                }
        tpl["notes"].append(entry)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(tpl, fh, ensure_ascii=False, indent=2)


def load_template(path: str) -> dict:
    import json
    if not os.path.exists(path):
        sys.exit(f"error: template file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(f"error: template is not valid JSON ({path}): {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("notes"), list):
        sys.exit(f"error: template has no 'notes' list — is {path} really a "
                 "file saved with --save-template?")
    return data


def _sorted_words(page) -> list:
    """Page words in visual reading order (top-to-bottom, left-to-right).

    The PDF's internal text-object order can be scrambled (headings emitted
    after their content); sorting by position restores what a reader sees.
    """
    words = page.get_text("words")
    return sorted(words, key=lambda w: (round(w[1], 1), w[0]))


def _page_token_index(pdf_path: str) -> list[tuple[int, list[str]]]:
    """[(page_no, tokens)] for every page of a PDF, in visual reading order."""
    doc = fitz.open(pdf_path)
    out = []
    try:
        for pno in range(len(doc)):
            out.append((pno + 1, [w[4] for w in _sorted_words(doc[pno])]))
    finally:
        doc.close()
    return out


def _anchor_scan(toks: list[str], anchor: list[str], lo: int, hi: int) -> tuple[float, Optional[int]]:
    """Best (ratio, index) placement of a short anchor within toks[lo..hi], step 1."""
    k = len(anchor)
    best: tuple[float, Optional[int]] = (0.0, None)
    for i in range(max(0, lo), min(len(toks) - k, hi) + 1):
        r = SequenceMatcher(None, anchor, toks[i:i + k], autojunk=False).ratio()
        if r > best[0]:
            best = (r, i)
    return best


def _pin_bounds(toks: list[str], target: list[str], s: int, W: int) -> tuple[int, int]:
    """Pin a coarse window to word-exact boundaries using the target's own
    opening and closing words as anchors (searched at stride 1)."""
    k = min(6, len(target))
    if k < 2:
        return s, W
    span = max(12, len(target) // 4)
    r_head, hs = _anchor_scan(toks, target[:k], s - span, s + span)
    r_tail, ts = _anchor_scan(toks, target[-k:], s + W - k - span, s + W - k + span)
    start = hs if (hs is not None and r_head >= 0.55) else s
    end = ts + k if (ts is not None and r_tail >= 0.55) else s + W
    if end - start < max(5, len(target) // 3):  # anchors collapsed — keep coarse
        return s, W
    # drop stray edge words that don't belong to the template's boundaries
    # (e.g. table cells preceding the paragraph in reading order)
    head_words = {w.lower() for w in target[:8]}
    tail_words = {w.lower() for w in target[-8:]}
    trims = 0
    while trims < 4 and end - start > 5 and toks[start].lower() not in head_words:
        start += 1
        trims += 1
    trims = 0
    while trims < 4 and end - start > 5 and toks[end - 1].lower() not in tail_words:
        end -= 1
        trims += 1
    return start, end - start


def locate_passage(pages: list[tuple[int, list[str]]], target: str) -> Optional[dict]:
    """Find the span of a document that best matches a template passage.

    Searches one flat token stream across the whole document (so passages that
    cross a page break still match), scores windows with SequenceMatcher,
    refines the best window, then pins word-exact boundaries. Returns
    {"conf": 0..1, "page": int, "endPage": int, "text": str} or None.
    """
    t = target.split()
    L = len(t)
    if not L:
        return None
    flat: list[str] = []
    pmap: list[int] = []
    for pno, toks in pages:
        flat.extend(toks)
        pmap.extend([pno] * len(toks))
    n = len(flat)
    if n == 0:
        return None
    W = min(n, L)
    stride = max(1, L // 5)
    best = (0.0, None)
    for s in range(0, max(1, n - W + 1), stride):
        sm = SequenceMatcher(None, t, flat[s:s + W], autojunk=False)
        if sm.real_quick_ratio() <= best[0]:
            continue
        r = sm.ratio()
        if r > best[0]:
            best = (r, s)
    if best[1] is None:
        return None
    r, s = best
    # refine boundaries around the winning window
    step = max(1, L // 16)
    span = max(step, L // 8)
    for ds in range(-span, span + 1, step):
        for dw in range(-span, span + 1, step):
            s2, W2 = max(0, s + ds), max(5, W + dw)
            if s2 + W2 > n:
                continue
            r2 = SequenceMatcher(None, t, flat[s2:s2 + W2], autojunk=False).ratio()
            if r2 > r:
                r, s, W = r2, s2, W2
    # word-exact edges: align to the template's opening/closing words
    s, W = _pin_bounds(flat, t, s, W)
    r = SequenceMatcher(None, t, flat[s:s + W], autojunk=False).ratio()
    return {"conf": r, "page": pmap[s], "endPage": pmap[min(s + W - 1, n - 1)],
            "text": " ".join(flat[s:s + W])}


def apply_template(template: dict, docs: list[tuple[str, str]],
                   marks_by_doc: dict[str, list[Mark]],
                   min_conf: float = 0.6) -> dict[str, list[dict]]:
    """Fill in template notes that are not already highlighted in each document.

    For every (note, statement) in the template: keep the reviewer's own
    highlight when one carries that serial; otherwise fuzzy-locate the passage
    in the PDF and synthesize a Mark (tagged with its confidence). Returns a
    per-document log of what was located/kept/not found.
    """
    log: dict[str, list[dict]] = {}
    page_index: dict[str, list] = {}
    paths = dict(docs)
    # a template passage keyed to a label that matches no document would be
    # silently skipped — surface the mismatch instead
    tpl_labels = {lbl for e in template.get("notes", []) for lbl in e.get("passages", {})}
    unmatched = tpl_labels - set(paths)
    if unmatched:
        print(f"    warning: template statement labels not in this run "
              f"(their notes are skipped): {', '.join(sorted(unmatched))}")
    for lbl, _ in docs:
        have = {m.serial_key for m in marks_by_doc.get(lbl, []) if m.serial_key}
        log[lbl] = []
        for entry in template.get("notes", []):
            if entry.get("serial") is None:
                continue
            serial = str(entry["serial"])
            passage = entry.get("passages", {}).get(lbl)
            if passage is None:
                continue  # template says this note is not present in this statement
            if serial in have:
                log[lbl].append({"serial": serial, "action": "kept-highlight"})
                continue
            if lbl not in page_index:
                page_index[lbl] = _page_token_index(paths[lbl])
            hit = locate_passage(page_index[lbl], passage.get("text", ""))
            if hit and hit["conf"] >= min_conf:
                marks_by_doc.setdefault(lbl, []).append(Mark(
                    doc=lbl, page=hit["page"],
                    serial_raw=passage.get("serial_raw", serial),
                    serial_key=serial,
                    text=hit["text"],
                    heading=entry.get("section", ""),
                    conf=round(hit["conf"], 3),
                ))
                log[lbl].append({"serial": serial, "action": "located",
                                 "conf": round(hit["conf"], 3), "page": hit["page"]})
            else:
                log[lbl].append({"serial": serial, "action": "not-found",
                                 "conf": round(hit["conf"], 3) if hit else 0.0})
    return log


def _best_word_window(words: list, target_tokens: list[str]) -> tuple[float, int, int]:
    """Best (ratio, start, width) window over a page's word list for target tokens."""
    toks = [w[4] for w in words]
    L, n = len(target_tokens), len(toks)
    if not L or not n:
        return 0.0, 0, 0
    best = (0.0, 0, min(n, L))
    W = min(n, L)
    stride = max(1, L // 6)
    for s in range(0, max(1, n - W + 1), stride):
        sm = SequenceMatcher(None, target_tokens, toks[s:s + W], autojunk=False)
        if sm.real_quick_ratio() <= best[0]:
            continue
        r = sm.ratio()
        if r > best[0]:
            best = (r, s, W)
    r, s, W = best
    step = max(1, L // 16)
    span = max(step, L // 8)
    for ds in range(-span, span + 1, step):
        for dw in range(-span, span + 1, step):
            s2, W2 = max(0, s + ds), max(3, W + dw)
            if s2 + W2 > n:
                continue
            r2 = SequenceMatcher(None, target_tokens, toks[s2:s2 + W2], autojunk=False).ratio()
            if r2 > r:
                r, s, W = r2, s2, W2
    s, W = _pin_bounds(toks, target_tokens, s, W)
    r = SequenceMatcher(None, target_tokens, toks[s:s + W], autojunk=False).ratio()
    return r, s, W


def annotate_pdf(src_path: str, dst_path: str, marks: list[Mark],
                 include_own: bool = False) -> int:
    """Write highlight annotations (serial number in the comment box) into a copy
    of the PDF for every auto-located mark. Returns the number added.

    The reviewer's own highlights are already in the file; only marks synthesized
    from a template (mark.conf set) are drawn unless include_own=True.
    """
    doc = fitz.open(src_path)
    added = 0
    try:
        for m in marks:
            if m.serial_key is None or (m.conf is None and not include_own):
                continue
            try:
                # combine the start page and the next page so passages that
                # cross a page break are drawn in full; hold Page objects —
                # a temporary page is deallocated before annot.update() runs
                pages = {pno: doc[pno - 1] for pno in (m.page, m.page + 1)
                         if 1 <= pno <= len(doc)}
                entries = []  # (page_no, word)
                for pno, pg in pages.items():
                    for w in _sorted_words(pg):
                        entries.append((pno, w))
                target = m.text.split()
                toks = [e[1][4] for e in entries]
                # exact token-sequence match first: the located text came from this
                # same word stream, so this guarantees highlight == report text
                s = -1
                for i in range(len(toks) - len(target) + 1):
                    if toks[i:i + len(target)] == target:
                        s = i
                        break
                if s >= 0:
                    sel = entries[s:s + len(target)]
                else:  # fall back to fuzzy matching (e.g. reviewer's own marks)
                    r, s, W = _best_word_window(toks, target)
                    sel = entries[s:s + W]
                    if not sel or r < 0.4:
                        continue
                # merge only horizontally-adjacent words on the same line, so the
                # highlight never sweeps across a table-column gap onto words
                # that are not part of the note
                per_page: dict[int, list[fitz.Rect]] = {}
                prev_page = prev_y = None
                prev_rect: Optional[fitz.Rect] = None
                for pno, w in sel:
                    ykey = round(w[1], 1)
                    rct = fitz.Rect(w[:4])
                    rects = per_page.setdefault(pno, [])
                    if (prev_rect is not None and prev_page == pno and prev_y == ykey
                            and 0 <= rct.x0 - prev_rect.x1 <= 14):
                        prev_rect.include_rect(rct)  # extend the run in place
                    else:
                        rects.append(rct)
                        prev_page, prev_y, prev_rect = pno, ykey, rct
                for pno, rects in per_page.items():
                    annot = pages[pno].add_highlight_annot(rects)
                    annot.set_info(content=m.serial_raw or m.serial_key,
                                   title=MACHINE_AUTHOR)
                    annot.update()
                added += 1
            except Exception as exc:  # one bad note must not lose the whole file
                print(f"    warning: could not draw serial {m.serial_key} "
                      f"on page {m.page} of {os.path.basename(src_path)}: {exc}")
        doc.save(dst_path)
    finally:
        doc.close()
    return added


def text_consistency(row: NoteRow) -> float:
    """Lowest pairwise similarity of highlighted text among present cells (0..1)."""
    texts = [m.text for m in row.cells.values() if m and m.text]
    if len(texts) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            worst = min(worst, SequenceMatcher(None, texts[i].split(), texts[j].split(), autojunk=False).ratio())
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
/* word-level diff: highlight ONLY the differing words, never the whole line
   blue = extra in this statement · orange = benchmark insertion */
.add{background:var(--accent-soft);color:var(--accent)}
.del{background:var(--warn-soft);color:var(--warn)}
.dtext.diffed{background:none}                         /* drop the full-line highlighter */
.dtext .add{background:var(--accent-soft);color:var(--accent);border-radius:3px;
  padding:0 2px;font-weight:700}
.dtext .del{background:var(--warn-soft);color:var(--warn);border-radius:3px;
  padding:0 2px;font-weight:600;border-bottom:1.5px dashed var(--warn)}

/* Track changes / Before / After: same markup, only the strikethrough moves.
   Before = orange insertions cut (not yet in force); After = blue extras cut. */
.dclip[data-view="before"] .dtext .del{text-decoration:line-through;
  text-decoration-thickness:1.6px;opacity:.72}
.dclip[data-view="after"] .dtext .add{text-decoration:line-through;
  text-decoration-thickness:1.6px;opacity:.72}
.viewbar{display:flex;gap:4px;align-items:center}
.vbtn{font:inherit;font-size:10.5px;font-weight:700;cursor:pointer;border-radius:7px;
  padding:3px 9px;border:1px solid var(--line-strong);background:transparent;color:var(--muted);
  transition:all .15s ease;letter-spacing:.01em}
.vbtn:hover{border-color:var(--accent);color:var(--accent)}
.vbtn.on{background:var(--accent);border-color:var(--accent);color:#fff;
  box-shadow:0 2px 8px color-mix(in srgb,var(--accent) 30%,transparent)}
.gview{display:flex;gap:4px;align-items:center}
.gview .gvlabel{font-size:11.5px;color:var(--muted);font-weight:650;margin-right:4px}
.difflegend{font-size:11px;color:var(--muted);line-height:1.5}
.difflegend .dh{margin-bottom:4px}
.difflegend .add,.difflegend .del{border-radius:3px;padding:0 5px;font-weight:700}
.difflegend .add{background:var(--accent-soft);color:var(--accent)}
.difflegend .del{background:var(--warn-soft);color:var(--warn)}
.benchtag{margin-left:auto;font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;
  color:#fff;background:var(--ink);padding:2px 8px;border-radius:999px}
.benchtag.fb{background:var(--warn);color:#fff}
.dcell.isbench{background:color-mix(in srgb,var(--accent) 7%,var(--card));
  box-shadow:inset 3px 0 0 var(--accent)}
.dcell.absent{background:repeating-linear-gradient(45deg,transparent,transparent 7px,
  color-mix(in srgb,var(--faint) 8%,transparent) 7px,color-mix(in srgb,var(--faint) 8%,transparent) 14px)}
.mini.muted{background:transparent;border:1px dashed var(--line-strong);color:var(--faint)}
.mini.tmpl{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);
  border-color:transparent;font-weight:700}
a.mini.pglink{text-decoration:none;cursor:pointer;color:var(--accent);
  border:1px solid color-mix(in srgb,var(--accent) 45%,transparent);
  transition:all .15s ease}
a.mini.pglink:hover{background:var(--accent);color:#fff;border-color:var(--accent);
  transform:translateY(-1px);box-shadow:0 3px 8px color-mix(in srgb,var(--accent) 35%,transparent)}
a.badge-serial{text-decoration:none;cursor:pointer;
  transition:transform .18s cubic-bezier(.34,1.56,.64,1),box-shadow .18s}
a.badge-serial:hover{transform:scale(1.12) rotate(-3deg);
  box-shadow:0 6px 18px color-mix(in srgb,var(--accent) 55%,transparent)}
a.badge-serial:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.tag-absent{margin-top:auto;font-size:11px;font-weight:650;color:var(--faint);line-height:1.4}
.hann{margin-top:5px;font-size:11.5px;font-weight:600;color:var(--warn);
  background:var(--warn-soft);display:inline-block;padding:3px 9px;border-radius:7px}
.tag-bench{margin-top:auto;font-size:11px;font-weight:650;color:var(--accent);line-height:1.4}
.dcell[data-state="rejected"]{box-shadow:inset 3px 0 0 var(--bad)}
.dcell[data-state="accepted"]{box-shadow:inset 3px 0 0 var(--good)}
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
      '<p class="intro">Every common note is benchmarked against <strong>'+esc(benchShort())+'</strong>'+
      (DATA.fallbackBenchmark?' (or <strong>'+esc(docShort(DATA.fallbackBenchmark))+'</strong> when the note is absent from it)':'')+'. '+
      'Where a statement <strong>differs</strong>, the note shows track changes — '+
      '<span class="del" style="padding:0 4px;border-radius:3px">orange</span> words are benchmark wording coming in; '+
      '<span class="add" style="padding:0 4px;border-radius:3px">blue</span> words exist here but not in the benchmark. '+
      'Toggle <strong>Before</strong> to cut the orange (change not yet made) or <strong>After</strong> to cut the blue (change incorporated) — per note or all at once. '+
      'A note that is <em>not highlighted</em> in a statement is treated as <strong>not present</strong> there — expected, no review needed. '+
      'Accept an acceptable variation or reject one that needs correction. Decisions are saved in this browser and can be exported. '+
      '<strong>Click a serial badge or a p.N chip</strong> to open that PDF at the exact page — keep this report in the same folder as the PDFs.</p>'+
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
        '<div class="gview" id="gview" role="group" aria-label="view all notes as">'+
          '<span class="gvlabel">View all</span>'+
          '<button class="vbtn on" data-gview="markup">Track changes</button>'+
          '<button class="vbtn" data-gview="before">Before</button>'+
          '<button class="vbtn" data-gview="after">After</button>'+
        '</div>'+
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
    // rehydrate saved decisions into the cell buttons (persistence across reloads)
    DATA.notes.forEach(function(n){ n.cells.forEach(function(c){
      if(c.differs && store[key(n.serial,c.doc)]) syncCell(n.serial,c.doc); }); });
    refresh();
  }
  function kpi(cls,n,l){ return '<div class="kpi '+cls+'"><div class="n">'+n+'</div><div class="l">'+l+'</div></div>'; }

  function noteCard(n){
    var cells = n.cells.map(function(c){ return cell(n,c); }).join('');
    var bulk = n.hasDiff ? '<div class="bulk">'+
        '<button data-bulk="accepted" data-serial="'+esc(n.serial)+'">Accept all</button>'+
        '<button data-bulk="rejected" data-serial="'+esc(n.serial)+'">Reject all</button></div>' : '';
    var notes = [];
    if(n.absentDocs && n.absentDocs.length)
      notes.push('Not present in: '+n.absentDocs.map(docShort).join(', '));
    if(n.fallbackUsed)
      notes.push('Benchmarked against '+docShort(n.benchmarkDoc)+' (absent from '+docShort(BENCH)+')');
    var ann = notes.length ? '<div class="hann">'+esc(notes.join('  ·  '))+'</div>' : '';
    // the serial badge links to the note in its benchmark statement's PDF
    var bcell = n.cells.find(function(c){ return c.isBenchmark && c.present; });
    var bmeta = bcell ? docMeta(bcell.doc) : null;
    var badge = (bcell && bmeta && bmeta.href)
      ? '<a class="badge-serial" href="'+esc(bmeta.href)+'#page='+bcell.page+'" target="_blank" rel="noopener" '+
        'title="Open the benchmark ('+esc(bmeta.main)+' '+esc(bmeta.sub||'')+') at page '+bcell.page+'">'+esc(n.serial)+'</a>'
      : '<span class="badge-serial">'+esc(n.serial)+'</span>';
    return '<article class="notecard" data-sev="'+n.severity+'" data-serial="'+esc(n.serial)+'" data-hasdiff="'+(n.hasDiff?1:0)+'">'+
      '<div class="note-head">'+
        badge+
        '<div class="htext"><div class="hlabel">'+esc(n.label)+'</div>'+
          '<div class="hsec">'+esc(n.section)+'</div>'+ann+'</div>'+
        '<div class="hstat"><span class="statpill" data-stat></span>'+bulk+'</div>'+
      '</div>'+
      '<div class="doc-grid" style="--cols:'+COLS+'">'+cells+'</div>'+
    '</article>';
  }

  var BENCH = DATA.benchmark;
  function docShort(label){ if(!label) return 'the majority version'; var m=docMeta(label); return m.main + (m.sub?(' '+m.sub):''); }
  function benchShort(){ return docShort(BENCH); }

  function cell(n,c){
    var meta = docMeta(c.doc);
    var refName = docShort(n.benchmarkDoc);
    var badge = c.isBenchmark ? ('<span class="benchtag'+(n.fallbackUsed?' fb':'')+'">Benchmark'+(n.fallbackUsed?' (fallback)':'')+'</span>') : '';
    var name = '<div class="dname">'+esc(meta.main)+(meta.sub?'<small>'+esc(meta.sub)+'</small>':'')+'</div>';
    var body, chips='', decision='';
    if(c.absent){
      // Note not highlighted here = not present in this statement (expected).
      chips = '<span class="mini muted">not present</span>';
      body = '<div class="dtext" style="background:none;color:var(--faint);font-style:italic">This note is not present in this statement — no review needed.</div>';
      decision = '<div class="tag-absent">Not applicable here</div>';
      return '<div class="dcell absent" data-serial="'+esc(n.serial)+'" data-doc="'+esc(c.doc)+'" data-differs="0">'+
        '<div class="dtop">'+name+'</div><div class="chiprow">'+chips+'</div>'+body+decision+'</div>';
    }
    var scls = c.reasons.indexOf('serial format')>=0 ? 'mini ser warn' : 'mini ser';
    var pageChip = meta.href
      ? '<a class="mini pglink" href="'+esc(meta.href)+'#page='+c.page+'" target="_blank" rel="noopener" '+
        'title="Open '+esc(meta.main)+' '+esc(meta.sub||'')+' at page '+c.page+'">p.'+c.page+' ↗</a>'
      : '<span class="mini">p.'+c.page+'</span>';
    chips = '<span class="'+scls+'">serial '+esc(c.serial||'—')+'</span>'+
            pageChip+
            (c.autoFound ? '<span class="mini tmpl">◎ auto-located '+Math.round(c.autoFound*100)+'%</span>' : '')+
            (c.reasons.indexOf('text differs')>=0 ? '<span class="mini bad">'+Math.round(c.sim*100)+'% match</span>' : '');
    // Benchmark shows its own text as the reference; differing statements keep the
    // track-changes markup in every view — the toggle only moves the strikethrough:
    // Before cuts the orange insertions (not yet in force), After cuts the blue extras.
    if(c.differs && !c.isBenchmark){
      body = '<div class="viewbar" role="group" aria-label="view">'+
          '<button class="vbtn on" data-view="markup" title="All changes visible, nothing cut">Track changes</button>'+
          '<button class="vbtn" data-view="before" title="Before the change — benchmark insertions (orange) shown cut">Before</button>'+
          '<button class="vbtn" data-view="after" title="After the change — extra words (blue) shown cut">After</button>'+
        '</div>'+
        '<div class="dclip" data-view="markup">'+
          '<span class="dtext diffed">'+diffHTML(n.refText, c.text)+'</span>'+
        '</div>'+
        '<button class="expand" data-exp>Show full text ▾</button>';
    } else {
      body = '<div class="dclip"><span class="dtext">'+esc(c.text)+'</span></div>'+
             '<button class="expand" data-exp>Show full text ▾</button>';
    }
    if(c.isBenchmark){
      decision = '<div class="tag-bench">Reference statement'+(n.fallbackUsed?' (used because the note is absent from '+esc(benchShort())+')':'')+' · all others compared to this</div>';
    } else if(c.differs){
      var counts = diffCounts(n.refText, c.text);
      var legend = '<div class="dh"><span class="del">insertions from benchmark ('+counts.del+')</span>'+
                   ' · <span class="add">deletions — extra here ('+counts.add+')</span></div>';
      decision = '<div class="decision">'+
        '<div class="difflegend">'+legend+'Highlighted above vs <strong>'+esc(refName)+'</strong>.</div>'+
        '<div class="seg" role="group" aria-label="decision">'+
          '<button class="acc" data-dec="accepted" aria-pressed="false"><span class="pop">✓</span> Accept</button>'+
          '<button class="rej" data-dec="rejected" aria-pressed="false"><span class="pop">✕</span> Reject</button>'+
        '</div></div>';
    } else {
      decision = '<div class="tag-match">Matches benchmark</div>';
    }
    return '<div class="dcell'+(c.isBenchmark?' isbench':'')+'" data-serial="'+esc(n.serial)+'" data-doc="'+esc(c.doc)+'" data-differs="'+(c.differs?1:0)+'">'+
      '<div class="dtop">'+name+badge+'</div>'+
      '<div class="chiprow">'+chips+'</div>'+
      body + decision +
    '</div>';
  }
  function diffCounts(ref,cur){ var d=wordDiff(ref,cur), a=0, x=0;
    d.forEach(function(p){ if(p.t.trim()==='') return; if(p.op==='+')x++; else if(p.op==='-')a++; }); return {add:x, del:a}; }

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
      if(!n.hasDiff){ pill.className='statpill '+(n.severity||'good'); pill.textContent=n.status||'Consistent'; return; }
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
      var vb=t.closest('[data-view]');
      if(vb){ var cellEl=vb.closest('.dcell'); var v=vb.getAttribute('data-view');
        cellEl.querySelector('.dclip').setAttribute('data-view', v);
        cellEl.querySelectorAll('.vbtn[data-view]').forEach(function(x){ x.classList.toggle('on', x===vb); });
        return; }
      var gv=t.closest('[data-gview]');
      if(gv){ var v2=gv.getAttribute('data-gview');
        document.querySelectorAll('#gview .vbtn').forEach(function(x){ x.classList.toggle('on', x===gv); });
        app.querySelectorAll('.dclip[data-view]').forEach(function(cl){ cl.setAttribute('data-view', v2); });
        app.querySelectorAll('.dcell .vbtn[data-view]').forEach(function(x){
          x.classList.toggle('on', x.getAttribute('data-view')===v2); });
        return; }
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
    if(c.absent) return 'not-present';
    if(c.isBenchmark) return 'benchmark';
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


def _display_section(heading: str, ref_text: str) -> str:
    """Prefer the captured heading; fall back to the note's opening words when
    the heading is missing or garbled (vertical/watermark text caught in the
    heading clip produces strings of 1-2 character fragments)."""
    h = _clean(heading)
    toks = h.split()
    if toks:
        tiny = sum(1 for t in toks if len(t) <= 2)
        if len(h) >= 12 and tiny / len(toks) <= 0.34:
            return h[:140]
    words = ref_text.split()
    if words:
        return " ".join(words[:12]) + ("…" if len(words) > 12 else "")
    return h


def build_payload(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]],
                  notes: list[NoteRow], benchmark: Optional[str] = None,
                  doc_links: Optional[dict[str, str]] = None) -> dict:
    """Assemble the JSON model the interactive front-end renders from.

    Every note is compared against the *benchmark* statement (default: the
    Consolidated Ind AS financial). The benchmark cell is the reference and is
    never flagged; other statements differ when their serial or highlighted text
    departs from the benchmark's.
    """
    from collections import Counter

    def word_diff_counts(ref: str, cur: str) -> tuple[int, int]:
        """(extra, missing) word counts of cur vs ref, case- and punctuation-sensitive."""
        a, b = ref.split(), cur.split()
        add = dele = 0
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag == "replace":
                dele += i2 - i1
                add += j2 - j1
            elif tag == "delete":
                dele += i2 - i1
            elif tag == "insert":
                add += j2 - j1
        return add, dele

    total = len(doc_labels)
    bench = resolve_benchmark(doc_labels, benchmark)
    fallback = resolve_fallback_benchmark(doc_labels, bench)
    common = [n for n in notes if n.present_count == total]
    partial = [n for n in notes if n.present_count < total]

    docs_meta = []
    for lbl in doc_labels:
        main, _, tail = lbl.partition("·")
        docs_meta.append({"label": lbl, "main": main.strip(), "sub": tail.strip(),
                          "benchmark": lbl == bench,
                          "href": (doc_links or {}).get(lbl)})

    note_objs = []
    for n in notes:  # already sorted by serial number
        # Per-note reference: the primary benchmark if it highlights this note;
        # otherwise the fallback (IFRS INR Consolidated); otherwise majority vote.
        if n.cells.get(bench):
            eff_bench = bench
        elif fallback and n.cells.get(fallback):
            eff_bench = fallback
        else:
            eff_bench = None

        bmark = n.cells.get(eff_bench) if eff_bench else None
        if bmark:
            ref_serial, ref_text, ref_source = bmark.serial_raw, bmark.text, eff_bench
        else:
            present_marks = [m for m in n.cells.values() if m]
            serials_raw = [m.serial_raw for m in present_marks if m.serial_raw]
            texts = [m.text for m in present_marks if m.text]
            ref_serial = Counter(serials_raw).most_common(1)[0][0] if serials_raw else ""
            ref_text = Counter(texts).most_common(1)[0][0] if texts else ""
            ref_source = None

        cells = []
        for lbl in doc_labels:
            is_bench = (lbl == eff_bench)
            m = n.cells[lbl]
            if m is None:
                # Not highlighted here = the note is not present in this statement.
                # That is expected/not applicable, not a difference to reconcile.
                cells.append({
                    "doc": lbl, "present": False, "page": None, "serial": "",
                    "text": "", "differs": False, "absent": True,
                    "reasons": ["not present in this statement"],
                    "sim": 0.0, "isBenchmark": False,
                })
                continue
            reasons = []
            sim = (SequenceMatcher(None, ref_text.split(), m.text.split(),
                                   autojunk=False).ratio() if ref_text else 1.0)
            add, dele = word_diff_counts(ref_text, m.text) if ref_text else (0, 0)
            if not is_bench:
                if (m.serial_raw or "").strip() != (ref_serial or "").strip():
                    reasons.append("serial format")
                if add or dele:  # ANY word-level difference, however small (e.g. case)
                    reasons.append("text differs")
            cells.append({
                "doc": lbl, "present": True, "page": m.page, "serial": m.serial_raw,
                "text": m.text, "differs": bool(reasons), "reasons": reasons,
                "sim": round(sim, 3), "wordAdd": add, "wordDel": dele,
                "isBenchmark": is_bench, "absent": False, "autoFound": m.conf,
            })

        diff_cells = [c for c in cells if c["differs"]]
        absent_docs = [c["doc"] for c in cells if c.get("absent")]
        present_n = total - len(absent_docs)
        if diff_cells:
            label, severity, has_diff = "Needs review", "warn", True
        elif absent_docs:
            label, severity, has_diff = f"Aligned · in {present_n} of {total}", "good", False
        else:
            label, severity, has_diff = "Consistent", "good", False
        note_objs.append({
            "serial": n.serial_key, "label": n.label,
            "section": _display_section(n.section, ref_text),
            "refSerial": ref_serial, "refText": ref_text, "refSource": ref_source,
            "benchmarkDoc": eff_bench, "fallbackUsed": eff_bench not in (None, bench),
            "absentDocs": absent_docs, "presentCount": present_n,
            "status": label, "severity": severity, "hasDiff": has_diff,
            "diffCount": len(diff_cells), "cells": cells,
        })

    unnumbered = []
    for lbl in doc_labels:
        for m in sorted((x for x in marks_by_doc[lbl] if x.serial_key is None), key=lambda x: x.page):
            unnumbered.append({"doc": lbl, "page": m.page, "text": (m.heading or m.text)[:160]})

    fully_consistent = sum(1 for n in note_objs if not n["hasDiff"])
    return {
        "generated": date.today().isoformat(),
        "benchmark": bench,
        "fallbackBenchmark": fallback,
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
                notes: list[NoteRow], benchmark: Optional[str] = None,
                doc_links: Optional[dict[str, str]] = None) -> str:
    payload = build_payload(doc_labels, marks_by_doc, notes, benchmark=benchmark,
                            doc_links=doc_links)
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

# characters that are text in a PDF but illegal inside an XLSX cell
_ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _xls(value):
    """Sanitize a value for an Excel cell: strip illegal control characters
    and stay under the 32,767-character cell limit."""
    if isinstance(value, str):
        return _ILLEGAL_XLSX.sub("", value)[:32000]
    return value


def load_decisions(path: str) -> dict[tuple[str, str], str]:
    """Read a decisions JSON exported from the HTML console -> {(serial, doc): decision}."""
    import json
    if not os.path.exists(path):
        sys.exit(f"error: decisions file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        sys.exit(f"error: decisions file is not valid JSON ({path}): {exc}")
    out: dict[tuple[str, str], str] = {}
    for n in data.get("notes", []):
        serial = str(n.get("serial"))
        for d in n.get("decisions", []):
            out[(serial, d.get("document"))] = (d.get("decision") or "").lower()
    return out


def render_xlsx(doc_labels: list[str], marks_by_doc: dict[str, list[Mark]],
                notes: list[NoteRow], out_path: str,
                decisions_path: Optional[str] = None,
                benchmark: Optional[str] = None) -> None:
    """Write a formatted workbook: common-notes matrix + unnumbered highlights.

    If decisions_path is given (a JSON exported from the interactive console),
    each financial cell is tagged and tinted with its Accept/Reject decision, a
    per-note Sign-off column is added, and a Decision Log sheet is appended.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        sys.exit("error: Excel export needs openpyxl — install with:  pip install openpyxl")

    total = len(doc_labels)
    common = [n for n in notes if n.present_count == total]
    partial = [n for n in notes if n.present_count < total]

    # Decisions + per-cell diff model (reuse the same logic the front-end uses).
    decisions = load_decisions(decisions_path) if decisions_path else {}
    have_dec = bool(decisions_path)
    payload = build_payload(doc_labels, marks_by_doc, notes, benchmark=benchmark)
    bench = payload["benchmark"]
    pnote_by_serial = {n["serial"]: n for n in payload["notes"]}

    def pcell(serial: str, doc: str) -> Optional[dict]:
        pn = pnote_by_serial.get(serial)
        if pn:
            for c in pn["cells"]:
                if c["doc"] == doc:
                    return c
        return None

    def decision_for(serial: str, doc: str) -> str:
        """'accepted' | 'rejected' | 'pending' | 'auto-match' | 'not-present' | 'benchmark'"""
        c = pcell(serial, doc)
        if c is None:
            return "auto-match"
        if c.get("absent"):
            return "not-present"
        if c.get("isBenchmark"):
            return "benchmark"
        if not c["differs"]:
            return "auto-match"
        return decisions.get((serial, doc)) or "pending"

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
    if have_dec:
        headers += ["Sign-off"]
    ncol = len(headers)
    status_col = 3 + total          # column holding structural "Status"
    signoff_col = status_col + 1    # column holding review "Sign-off" (if any)

    # Title banner
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    t = ws.cell(1, 1, "Common Notes — Highlight & Serial Consistency")
    t.font = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    t.fill = PatternFill("solid", fgColor=ACCENT)
    t.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[1].height = 30

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncol)
    s = ws.cell(2, 1, f"{total} statements · {len(common)} common notes · benchmarked against {bench} "
                      f"· generated {date.today().isoformat()}")
    s.font = Font(size=9, italic=True, color="5B6472")
    s.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 16

    # Header row (mark the benchmark statement)
    hr = 3
    for c, name in enumerate(headers, start=1):
        is_bench_col = name == bench
        cell = ws.cell(hr, c, (name + "  ★ benchmark") if is_bench_col else name)
        cell.font = Font(bold=True, color="FFFFFF" if is_bench_col else ACCENT, size=10)
        cell.fill = PatternFill("solid", fgColor=INK if is_bench_col else ACC_SOFT)
        cell.alignment = center
        cell.border = border
    ws.row_dimensions[hr].height = 34

    SEV_COL = {"good": (GOOD, GOOD_SOFT), "warn": (WARN, WARN_SOFT), "bad": (BAD, BAD_SOFT)}

    def status_text(n: NoteRow) -> tuple[str, str, str]:
        pn = pnote_by_serial.get(n.serial_key, {})
        col, fill = SEV_COL.get(pn.get("severity", "good"), (GOOD, GOOD_SOFT))
        return pn.get("status", "Consistent"), col, fill

    DEC_TAG = {"accepted": "✔ ACCEPTED", "rejected": "✘ REJECTED",
               "pending": "◻ PENDING", "auto-match": ""}
    DEC_FILL = {"accepted": GOOD_SOFT, "rejected": BAD_SOFT, "pending": WARN_SOFT}
    DEC_TXT = {"accepted": GOOD, "rejected": BAD, "pending": WARN}

    def signoff_text(n: NoteRow) -> tuple[str, str, str]:
        pn = pnote_by_serial.get(n.serial_key, {})
        diff_cells = [c for c in pn.get("cells", []) if c["differs"]]
        if not diff_cells:
            return "No review needed", "5B6472", "FFFFFF"
        acc = sum(1 for c in diff_cells if decisions.get((n.serial_key, c["doc"])) == "accepted")
        rej = sum(1 for c in diff_cells if decisions.get((n.serial_key, c["doc"])) == "rejected")
        pend = len(diff_cells) - acc - rej
        if pend:
            return f"Pending\n{acc} acc · {rej} rej · {pend} to do", WARN, WARN_SOFT
        if rej:
            return f"Signed off\n{acc} accepted · {rej} rejected", WARN, WARN_SOFT
        return f"Signed off\nall {acc} accepted", GOOD, GOOD_SOFT

    r = hr + 1
    for i, n in enumerate(notes):  # sequential serial order
        zebra = ZEBRA if i % 2 else "FFFFFF"
        ws.cell(r, 1, n.serial_key).font = Font(bold=True, color=ACCENT, size=12)
        ws.cell(r, 1).alignment = center
        ws.cell(r, 2, _xls(pnote_by_serial.get(n.serial_key, {}).get("section", n.section))).alignment = wrap_top
        for c, lbl in enumerate(doc_labels, start=3):
            m = n.cells[lbl]
            pc = pcell(n.serial_key, lbl) or {}
            if m is None:
                # Not present in this statement — expected, not an error.
                cell = ws.cell(r, c, "— not present in this statement —")
                cell.font = Font(italic=True, color="8A929E", size=9)
            elif pc.get("isBenchmark"):
                fb = " (fallback)" if pnote_by_serial[n.serial_key].get("fallbackUsed") else ""
                auto = f" ◎ auto-located {round(pc['autoFound']*100)}%" if pc.get("autoFound") else ""
                cell = ws.cell(r, c, _xls(f"★ BENCHMARK{fb}{auto} — [serial {m.serial_raw or '—'} · p.{m.page}]\n{m.text}"))
                cell.font = Font(size=9, color=ACCENT, bold=True)
                cell.fill = PatternFill("solid", fgColor=ACC_SOFT)
            else:
                dec = decision_for(n.serial_key, lbl) if have_dec else "auto-match"
                tag = DEC_TAG.get(dec, "")
                auto = f"◎ auto-located {round(pc['autoFound']*100)}% · " if pc.get("autoFound") else ""
                prefix = f"{tag} — " if tag else ""
                cell = ws.cell(r, c, _xls(f"{prefix}{auto}[serial {m.serial_raw or '—'} · p.{m.page}]\n{m.text}"))
                cell.font = Font(size=9, color=DEC_TXT.get(dec, INK),
                                 bold=dec in ("accepted", "rejected"))
                cell.fill = PatternFill("solid", fgColor=DEC_FILL.get(dec, HI))
            cell.alignment = wrap_top
        stxt, scol, sfill = status_text(n)
        sc = ws.cell(r, status_col, stxt)
        sc.font = Font(bold=True, color=scol, size=9)
        sc.fill = PatternFill("solid", fgColor=sfill)
        sc.alignment = center
        if have_dec:
            otxt, ocol, ofill = signoff_text(n)
            oc = ws.cell(r, signoff_col, otxt)
            oc.font = Font(bold=True, color=ocol, size=9)
            oc.fill = PatternFill("solid", fgColor=ofill)
            oc.alignment = center
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
    ws.column_dimensions[get_column_letter(status_col)].width = 16
    if have_dec:
        ws.column_dimensions[get_column_letter(signoff_col)].width = 20
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
            ws2.cell(rr, 3, _xls(m.heading or m.text)).alignment = wrap_top
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

    # ---------- Sheet 3: Decision Log (only when decisions supplied) ----------
    if have_dec:
        ws3 = wb.create_sheet("Decision Log")
        ws3.sheet_view.showGridLines = False
        h3 = ["Serial", "Note", "Statement", "Page", "Differs?", "Reason(s)", "Match %", "Decision"]
        for c, name in enumerate(h3, start=1):
            cell = ws3.cell(1, c, name)
            cell.font = Font(bold=True, color=ACCENT, size=10)
            cell.fill = PatternFill("solid", fgColor=ACC_SOFT)
            cell.alignment = center
            cell.border = border
        ws3.row_dimensions[1].height = 26
        rr = 2
        DEC_TAG_LOG = dict(DEC_TAG, benchmark="★ BENCHMARK", **{"not-present": "not present"})
        for pn in payload["notes"]:
            for c in pn["cells"]:
                dec = "benchmark" if c.get("isBenchmark") else decision_for(pn["serial"], c["doc"])
                vals = [pn["serial"], pn["label"], c["doc"],
                        "" if c["page"] is None else c["page"],
                        "yes" if c["differs"] else "no",
                        "; ".join(c["reasons"]),
                        f"{round(c['sim'] * 100)}%" if c["present"] else "",
                        DEC_TAG_LOG.get(dec, dec) or "—"]
                for c2, v in enumerate(vals, start=1):
                    cell = ws3.cell(rr, c2, _xls(v))
                    cell.border = border
                    cell.font = Font(size=9,
                                     color=DEC_TXT.get(dec, INK) if c2 == 8 else INK,
                                     bold=(c2 == 8 and dec in ("accepted", "rejected")))
                    cell.alignment = wrap_top if c2 in (2, 3, 6) else center
                    if c2 == 8 and dec in DEC_FILL:
                        cell.fill = PatternFill("solid", fgColor=DEC_FILL[dec])
                    elif rr % 2:
                        cell.fill = PatternFill("solid", fgColor=ZEBRA)
                ws3.row_dimensions[rr].height = 28
                rr += 1
        for col, w in zip("ABCDEFGH", (8, 26, 26, 8, 10, 22, 10, 16)):
            ws3.column_dimensions[col].width = w
        ws3.freeze_panes = "A2"

    wb.save(out_path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def detect_docs_in_dir(folder: str) -> list[tuple[str, str]]:
    """Scan a folder of PDFs and assign each to its statement by filename.

    Recognized patterns (case-insensitive), matching this package's naming:
      ifrs + inr   -> IFRS · INR Consolidated   (consolifrsinr… / IFRS_INR_…)
      ifrs + usd   -> IFRS · USD Earnings Release (ifrsusd… / IFRS_USD_…)
      sa… / standalone -> Ind AS · Standalone   (safy… / SA_FY…)
      consol…      -> Ind AS · Consolidated     (consolfy… / CONSOL_FY…)
    Exactly one file per statement is required.
    """
    import glob as _glob
    if not os.path.isdir(folder):
        sys.exit(f"error: --dir is not a folder: {folder}")
    pdfs = sorted(_glob.glob(os.path.join(folder, "*.pdf")))
    if not pdfs:
        sys.exit(f"error: no PDFs found in {folder}")

    def classify(path: str) -> Optional[str]:
        name = os.path.basename(path).lower()
        if "ifrs" in name and "inr" in name:
            return "IFRS · INR Consolidated"
        if "ifrs" in name and "usd" in name:
            return "IFRS · USD Earnings Release"
        if name.startswith(("sa_", "sa-", "safy")) or "standalone" in name or re.search(r"(^|[^a-z])sa[^a-z]?fy", name):
            return "Ind AS · Standalone"
        if "consol" in name:
            return "Ind AS · Consolidated"
        return None

    found: dict[str, list[str]] = {}
    skipped: list[str] = []
    for p in pdfs:
        lbl = classify(p)
        if lbl:
            found.setdefault(lbl, []).append(p)
        else:
            skipped.append(os.path.basename(p))

    order = ["Ind AS · Consolidated", "Ind AS · Standalone",
             "IFRS · INR Consolidated", "IFRS · USD Earnings Release"]
    problems = []
    for lbl in order:
        hits = found.get(lbl, [])
        if len(hits) > 1:
            problems.append(f"multiple PDFs match {lbl}: "
                            + ", ".join(os.path.basename(h) for h in hits))
    present = [lbl for lbl in order if found.get(lbl)]
    if len(present) < 2 and not problems:
        problems.append(f"only {len(present)} statement(s) recognized — at least 2 are "
                        "needed for a comparison")
    if problems:
        sys.exit("error: could not assign the folder's PDFs to statements:\n  - "
                 + "\n  - ".join(problems)
                 + ("\n  (unrecognized: " + ", ".join(skipped) + ")" if skipped else "")
                 + "\n  Use explicit --doc 'Label=path' arguments instead.")
    missing = [lbl for lbl in order if lbl not in present]
    if missing:
        print(f"  note: comparing {len(present)} statements — no PDF found for: "
              + ", ".join(missing))
    if skipped:
        print(f"  note: ignoring unrecognized PDFs in folder: {', '.join(skipped)}")
    return [(lbl, found[lbl][0]) for lbl in present]


def parse_docs(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for p in pairs:
        if "=" not in p:
            sys.exit(f"--doc must be 'Label=/path.pdf', got: {p}")
        label, path = p.split("=", 1)
        out.append((label.strip(), os.path.expanduser(path.strip())))
    labels = [l for l, _ in out]
    dupes = {l for l in labels if labels.count(l) > 1}
    if dupes:
        sys.exit(f"error: duplicate --doc labels: {', '.join(sorted(dupes))} — "
                 "each statement needs a unique label.")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Compare highlighted common notes across statements.",
        epilog="Simplest use: python common_notes_report.py FOLDER — runs everything "
               "(template auto-found next to this script, outputs in FOLDER/output).")
    ap.add_argument("folder", nargs="?", default=None,
                    help="One-command mode: folder with the PDFs. Implies --dir FOLDER, "
                         "auto-uses notes_template_final.json if present beside this "
                         "script (disable with --no-template), and writes report.html, "
                         "report.xlsx and highlighted PDFs into FOLDER/output.")
    ap.add_argument("--no-template", action="store_true",
                    help="In one-command mode, skip the template (pure comparison of "
                         "already-highlighted PDFs).")
    ap.add_argument("--doc", action="append", default=[],
                    help="Repeatable. Format: 'Label=/path/to.pdf' (column order preserved).")
    ap.add_argument("--dir", default=None, metavar="FOLDER",
                    help="Folder containing the four PDFs — statements are assigned "
                         "automatically by filename (consol…/sa…/…ifrsinr…/…ifrsusd…). "
                         "Alternative to --doc.")
    ap.add_argument("--out", default="common_notes_report.html", help="Output HTML file.")
    ap.add_argument("--xlsx", default=None,
                    help="Also write an Excel workbook to this path (e.g. report.xlsx).")
    ap.add_argument("--decisions", default=None,
                    help="Path to a decisions JSON exported from the HTML console. Folds each "
                         "Accept/Reject into the Excel (cell tags, Sign-off column, Decision Log).")
    ap.add_argument("--benchmark", default=DEFAULT_BENCHMARK,
                    help="Label of the statement to benchmark all others against "
                         f"(default: {DEFAULT_BENCHMARK!r}; auto-detects a consolidated Ind AS statement).")
    ap.add_argument("--save-template", default=None, metavar="PATH",
                    help="Learn from this (highlighted) set: save the common-note passages "
                         "as a reusable template JSON for future periods.")
    ap.add_argument("--template", default=None, metavar="PATH",
                    help="Locate the template's notes in these (unhighlighted) PDFs: any note a "
                         "document does not highlight itself is fuzzy-matched from the template.")
    ap.add_argument("--min-confidence", type=float, default=0.6,
                    help="Minimum match confidence (0-1) for a template-located note (default 0.6).")
    ap.add_argument("--annotate-dir", default=None, metavar="DIR",
                    help="Write highlighted copies of the PDFs here: every auto-located note is "
                         "drawn as a highlight with its serial number in the comment box.")
    args = ap.parse_args(argv)

    if args.folder:
        # One-command mode: fill in every unset option with sensible defaults.
        if args.dir or args.doc:
            sys.exit("error: give either a bare FOLDER or --dir/--doc, not both.")
        args.dir = args.folder
        outdir = os.path.join(args.folder, "output")
        if args.annotate_dir is None:
            args.annotate_dir = outdir
        if args.out == "common_notes_report.html":  # parser default untouched
            args.out = os.path.join(outdir, "report.html")
        if args.xlsx is None:
            args.xlsx = os.path.join(outdir, "report.xlsx")
        if args.template is None and not args.no_template:
            for cand in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "notes_template_final.json"),
                         "notes_template_final.json"):
                if os.path.exists(cand):
                    args.template = cand
                    break
        os.makedirs(outdir, exist_ok=True)
        print(f"  One-command mode: PDFs from {args.folder} → outputs in {outdir}"
              + (f"  (template: {os.path.basename(args.template)})" if args.template
                 else "  (no template — comparing the PDFs' own highlights)"))

    if args.dir and args.doc:
        sys.exit("error: use either --dir or --doc, not both.")
    if args.dir:
        docs = detect_docs_in_dir(os.path.expanduser(args.dir))
    else:
        docs = parse_docs(args.doc) if args.doc else DEFAULT_DOCS
    labels = [d[0] for d in docs]

    marks_by_doc: dict[str, list[Mark]] = {}
    for label, path in docs:
        marks = extract_marks(label, path)
        marks_by_doc[label] = marks
        numbered = sum(1 for m in marks if m.serial_key is not None)
        print(f"  {label:32s} {len(marks):3d} highlights  ({numbered} serial-numbered)")

    if args.template:
        template = load_template(args.template)
        log = apply_template(template, docs, marks_by_doc, min_conf=args.min_confidence)
        print(f"\n  Template: {args.template} ({len(template.get('notes', []))} notes)")
        for lbl in labels:
            located = [e for e in log[lbl] if e["action"] == "located"]
            kept = [e for e in log[lbl] if e["action"] == "kept-highlight"]
            missing = [e for e in log[lbl] if e["action"] == "not-found"]
            parts = []
            if kept:
                parts.append(f"{len(kept)} own highlights")
            if located:
                confs = ", ".join(f"{e['serial']}@{round(e['conf']*100)}%" for e in located)
                parts.append(f"{len(located)} auto-located ({confs})")
            if missing:
                parts.append(f"{len(missing)} NOT FOUND ({', '.join(e['serial'] for e in missing)})")
            print(f"    {lbl:32s} " + " · ".join(parts or ["nothing to locate"]))

    notes = build_notes(labels, marks_by_doc)
    common = [n for n in notes if n.present_count == len(labels)]
    bench = resolve_benchmark(labels, args.benchmark)
    print(f"\n  {len(notes)} distinct serials · {len(common)} common across all {len(labels)} statements")
    print(f"  Benchmark statement: {bench}")

    if args.save_template:
        save_template(args.save_template, labels, notes, args.benchmark)
        print(f"  Template saved to: {os.path.abspath(args.save_template)}")

    # Page-number links in the report open the PDF beside it (same folder):
    # the highlighted copy when --annotate-dir is used, else the source file.
    doc_links: dict[str, str] = {}
    if args.annotate_dir:
        os.makedirs(args.annotate_dir, exist_ok=True)
        print()
        for label, path in docs:
            base = os.path.splitext(os.path.basename(path))[0]
            dst = os.path.join(args.annotate_dir, f"{base}_highlighted.pdf")
            n_added = annotate_pdf(path, dst, marks_by_doc[label])
            doc_links[label] = os.path.basename(dst)
            print(f"  Highlighted PDF ({n_added} notes drawn): {dst}")
    else:
        for label, path in docs:
            doc_links[label] = os.path.basename(path)

    body = render_html(labels, marks_by_doc, notes, benchmark=args.benchmark,
                       doc_links=doc_links)
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
        render_xlsx(labels, marks_by_doc, notes, args.xlsx,
                    decisions_path=args.decisions, benchmark=args.benchmark)
        note = " (with Accept/Reject sign-off)" if args.decisions else ""
        print(f"  Excel report written to: {os.path.abspath(args.xlsx)}{note}")
    elif args.decisions:
        print("  Note: --decisions has no effect without --xlsx.")


if __name__ == "__main__":
    main()
