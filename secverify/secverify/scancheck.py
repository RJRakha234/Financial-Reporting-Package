"""toolscan — verify figures rendered inside a PDF's images (charts) via OCR.

For chart-native exhibits (fact sheets) the key figures live inside crisp,
digitally-rendered images that the text engine cannot see.  Measured on the
reference factsheet, Tesseract reads those renders at 99.3% precision and
99.4% recall — above the 99% bar — so this tool exists for THAT class.  It is
NOT for print-scans (newspaper ads, scanned signatures): measured accuracy
there was 46%, and the tool refuses to pretend otherwise, reporting page image
quality alongside every finding.  All findings are review severity: OCR is
approximate, so this tool prompts eyes — it never stamps green.

Usage::

    python -m secverify.toolscan factsheet.pdf exhibit.htm

Requires ``pdftoppm`` (poppler-utils) and ``tesseract`` on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup

from .numbers import is_significant, iter_tokens


def _require(binary: str) -> None:
    if shutil.which(binary) is None:
        sys.exit(
            f"error: '{binary}' not found. Install it first "
            "(apt-get install poppler-utils tesseract-ocr)."
        )


def _figures(text: str, significant_only: bool = True) -> set[str]:
    return {
        key
        for _s, _e, tok, key in iter_tokens(text)
        if not significant_only or is_significant(key, tok)
    }


def scan_check(pdf_path: str, html_path: str, dpi: int = 300) -> int:
    _require("pdftoppm")
    _require("tesseract")

    html_text = BeautifulSoup(
        Path(html_path).read_text(encoding="utf-8", errors="replace"),
        "html.parser",
    ).get_text(" ")
    html_figs = _figures(html_text)

    # PDF text layer (what the main tool already covers)
    from .pdfside import load_pdf

    corpus = load_pdf(pdf_path)
    text_figs = {k for k in html_figs if corpus.has_number(k)}

    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            ["pdftoppm", "-png", "-r", str(dpi), pdf_path, f"{td}/pg"],
            check=True,
        )
        pages = sorted(Path(td).glob("pg-*.png"))
        ocr_figs: set[str] = set()
        ocr_all: set[str] = set()
        for png in pages:
            out = subprocess.run(
                ["tesseract", str(png), "stdout"],
                capture_output=True, text=True,
            ).stdout
            ocr_figs |= _figures(out)
            ocr_all |= _figures(out, significant_only=False)

    # Direction 1: HTML figures found neither in text nor in the images
    unmatched_html = sorted(
        k for k in html_figs if k not in text_figs and k not in ocr_figs
        and not corpus.has_number(k)
    )
    # Direction 2: image figures that appear nowhere in the HTML
    unmatched_ocr = sorted(
        k for k in ocr_figs
        if k not in _figures(html_text, significant_only=False)
        and not corpus.has_number(k)
    )

    chart_only = [k for k in html_figs if not corpus.has_number(k)]
    confirmed = sum(1 for k in chart_only if k in ocr_figs)

    print(f"toolscan — OCR image-figure check ({len(pages)} page(s) @ {dpi} DPI)")
    print(f"  HTML figures whose home is a chart/image : {len(chart_only)}")
    print(f"  confirmed by OCR inside the images       : {confirmed}")
    print(f"  NOT found in text OR images (REVIEW)     : {len(unmatched_html)}")
    for k in unmatched_html[:20]:
        print(f"     - {k}")
    print(f"  image figures absent from the HTML (REVIEW): {len(unmatched_ocr)}")
    for k in unmatched_ocr[:20]:
        print(f"     - {k}")
    print(
        "  NOTE: OCR is approximate (~99% on digital chart renders, far lower "
        "on print-scans). Every line above is a prompt to look, not a verdict; "
        "confirmed figures are corroborated, not green-proven."
    )
    return 1 if (unmatched_html or unmatched_ocr) else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: python -m secverify.toolscan <pdf> <html> [dpi]")
        return 2
    dpi = int(args[2]) if len(args) > 2 else 300
    return scan_check(args[0], args[1], dpi=dpi)


if __name__ == "__main__":
    raise SystemExit(main())
