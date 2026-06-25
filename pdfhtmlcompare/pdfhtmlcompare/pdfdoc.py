"""Read a financial-statement PDF into logical lines, each carrying its figures.

Statements are laid out as whitespace-aligned columns rather than ruled tables,
so we work from the word boxes the PDF text carries: cluster words into rows by
their vertical position, glue space-separated thousands back together, and keep
every figure's bounding box so it can later be highlighted on the page.
"""

import os
import tempfile

import pdfplumber

from .model import Line, build_line
from .numbers import is_figure, is_numberish

_ROW_TOLERANCE = 3.0  # points: two words on the same line
_MERGE_GAP = 7.0      # points: gap below which two number tokens are one figure


def _cluster_rows(words: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        for row in rows:
            if abs(row[0]["top"] - word["top"]) <= _ROW_TOLERANCE:
                row.append(word)
                break
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _merge_numberish(words: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for word in words:
        if (
            merged
            and is_numberish(word["text"])
            and is_numberish(merged[-1]["text"])
            and word["x0"] - merged[-1]["x1"] <= _MERGE_GAP
        ):
            prev = merged[-1]
            prev["text"] = prev["text"] + " " + word["text"]
            prev["x1"] = word["x1"]
            prev["bottom"] = max(prev["bottom"], word["bottom"])
            prev["top"] = min(prev["top"], word["top"])
        else:
            merged.append(dict(word))
    return merged


def _page_lines(page_index: int, page, start_index: int) -> list[Line]:
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    lines: list[Line] = []
    idx = start_index
    for raw_row in _cluster_rows(words):
        merged = [w for w in _merge_numberish(raw_row) if w["text"].strip()]
        if not merged:
            continue
        triples = []
        for w in merged:
            text = w["text"].strip()
            real, value = is_figure(text)
            bbox = (w["x0"], w["top"], w["x1"], w["bottom"])
            triples.append((text, value if real else None, bbox))
        x0 = min(w["x0"] for w in merged)
        top = min(w["top"] for w in merged)
        x1 = max(w["x1"] for w in merged)
        bottom = max(w["bottom"] for w in merged)
        full = " ".join(w["text"].strip() for w in merged)
        lines.append(
            build_line(idx, full, triples, page=page_index, bbox=(x0, top, x1, bottom))
        )
        idx += 1
    return lines


def read_pdf_lines(pdf_path: str) -> list[Line]:
    """Every page's content as reading-ordered logical lines."""
    out: list[Line] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            out.extend(_page_lines(i, page, len(out)))
    return out


def merge_pdfs(paths: list[str]) -> str:
    """Concatenate PDFs (in order) into a temporary file; return its path.

    Lets the published document be supplied as several PDFs — e.g. the auditor's
    report and the financial statements — that together correspond to one filed
    HTML. Page numbers in the result then run continuously across them.
    """
    import fitz  # PyMuPDF

    out = fitz.open()
    for p in paths:
        src = fitz.open(p)
        out.insert_pdf(src)
        src.close()
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="pdfhtmlcompare_")
    os.close(fd)
    out.save(tmp)
    out.close()
    return tmp
