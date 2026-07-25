"""Why is a row-ORDER problem not being reported on THIS document?

The order checks are the only defence against a line that moves with its values
— every value check passes, so if the order check declines, the reordering is
invisible.  Each check declines for a specific, knowable reason (a label that
repeats, a row that cannot be located in the PDF, a table that cannot be pinned
to a PDF region), but none of that is visible in the review copy.

This prints the reason, per row, for one named table.

    python tools/diagnose_row_order.py statement.pdf [more.pdf ...] exhibit.htm
    python tools/diagnose_row_order.py ... --label "Hi-Tech"

With ``--label`` it reports only tables containing that text, which is usually
what you want: point it at the row you believe moved.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup  # noqa: E402

from secverify import grid  # noqa: E402
from secverify.pdfside import load_pdf, order_pdfs_to_html  # noqa: E402


def main(argv: list[str]) -> int:
    label = None
    if "--label" in argv:
        i = argv.index("--label")
        label = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) < 2:
        print(__doc__)
        return 2
    *pdfs, html_path = argv

    raw = open(html_path, encoding="utf-8", errors="replace").read()
    ordered, _ = order_pdfs_to_html(pdfs, raw)
    corpus = load_pdf(ordered)
    soup = BeautifulSoup(raw, "html.parser")

    # the PDF's rows, exactly as the order checks see them
    pdf_rows: list[tuple[str, list[str]]] = []
    for page in corpus.pages_raw:
        for line in page.splitlines():
            lbl, figs = grid.line_label_figs(line)
            if lbl and figs:
                pdf_rows.append((lbl, figs))
    pdf_pos: dict[str, list[int]] = {}
    for i, (l, _f) in enumerate(pdf_rows):
        pdf_pos.setdefault(l, []).append(i)

    geom = [
        r
        for pg in grid._load_pdf_pages(ordered)
        for r in grid._parse_pdf_rows(pg)
        if r[0] and r[1] and len(r[0]) >= 6
    ]
    gpos: dict[tuple, list[int]] = {}
    for i, (l, f) in enumerate(geom):
        gpos.setdefault(grid._row_key(l, f), []).append(i)
    print(f"PDF: {len(pdf_rows)} text rows, {len(geom)} geometry rows\n")

    tables = [
        t for t in soup.find_all("table")
        if label is None or label.lower() in t.get_text(" ").lower()
    ]
    if not tables:
        print(f"no table contains {label!r}")
        return 1

    for n, t in enumerate(tables, 1):
        rows = grid._parse_html_rows(t)
        rows = [(l, f) for l, f in rows if l and f]
        if not grid._is_statement_table(grid._parse_html_rows(t)):
            print(f"table {n}: NOT treated as a statement table "
                  f"(needs >= {grid.MIN_STATEMENT_ROWS} labelled figure rows) "
                  f"— no order check runs on it at all")
            continue
        hcnt = Counter(l for l, _f in rows)
        keys = Counter(grid._row_key(l, f) for l, f in rows)
        print(f"table {n}: {len(rows)} figure rows")
        print(f"  {'row label':38} {'figs':22} label-order?  (label,values)-order?")
        for l, f in rows:
            if label and label.lower().replace(" ", "").replace("-", "") not in l:
                pass
            # why the LABEL-sequence check would skip this row
            why_a = "yes"
            if len(l) < grid.ROW_ORDER_MIN_LABEL:
                why_a = "no: label too short"
            elif hcnt[l] != 1:
                why_a = f"no: label repeats {hcnt[l]}x in table"
            elif len(pdf_pos.get(l, [])) != 1:
                why_a = f"no: {len(pdf_pos.get(l, []))} PDF lines carry it"
            # why the (label,values) GEOMETRY check would skip it
            key = grid._row_key(l, f)
            why_b = "yes"
            if not geom:
                why_b = "no: no PDF geometry (unreadable/scanned?)"
            elif len(l) < 6:
                why_b = "no: label too short"
            elif keys[key] != 1:
                why_b = "no: identical row twice in table"
            elif len(gpos.get(key, [])) != 1:
                why_b = f"no: {len(gpos.get(key, []))} geometry rows match"
            print(f"  {l[:38]:38} {' '.join(f)[:22]:22} {why_a:24} {why_b}")
        print()

    print("Reading this: a row needs YES in at least one column to take part in")
    print("an order check. A row with NO in both is not order-checked at all —")
    print("if it moves, nothing will report it. The reason given is the fix:")
    print("  'label repeats'      -> the (label,values) check should cover it;")
    print("                          if that also says no, the values differ")
    print("                          between PDF and HTML or the row wrapped.")
    print("  'N PDF lines'        -> the label is ambiguous in the PDF.")
    print("  'no PDF geometry'    -> the PDF has no readable text layer.")
    print("  'N geometry rows'    -> 0 means the PDF's parsed row does not match")
    print("                          this HTML row: usually a wrapped label or a")
    print("                          footnote marker read as a figure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
