"""Sweep: which statement rows are NOT order-checked, and why — across filings.

A row that moves with its values passes every value check, so the order checks
are the only thing standing between a reordering and a green report.  Each order
check declines on specific rows for specific reasons, and none of that is visible
in the review copy — which is how a footnote-marker bug survived several rounds of
investigation while looking fully covered.

This asks the inverted question at scale: over many real filings, what fraction of
statement rows can no order check see, broken down by reason?  A reason that
accounts for a large share of rows is a bug of that same class waiting to be
found, not an inherent limit.

    python tools/order_coverage.py            # sweeps the built-in batch list
    python tools/order_coverage.py a.pdf b.htm  # one pair

Prints a per-exhibit coverage figure and an aggregate reason table.  Nothing is
mutated and nothing is written; this reads only.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup  # noqa: E402

from secverify import grid  # noqa: E402
from secverify.pdfside import load_pdf, order_pdfs_to_html  # noqa: E402


def analyse(pdfs: list[str], html_path: str) -> tuple[int, int, Counter]:
    """``(rows, order_checkable, reasons)`` for one exhibit."""
    raw = open(html_path, encoding="utf-8", errors="replace").read()
    ordered, _ = order_pdfs_to_html(pdfs, raw)
    corpus = load_pdf(ordered)
    soup = BeautifulSoup(raw, "html.parser")

    pdf_pos: dict[str, list[int]] = {}
    idx = 0
    for page in corpus.pages_raw:
        for line in page.splitlines():
            lbl, figs = grid.line_label_figs(line)
            if lbl and figs:
                pdf_pos.setdefault(lbl, []).append(idx)
                idx += 1

    geom = [
        r
        for pg in grid._load_pdf_pages(ordered)
        for r in grid._parse_pdf_rows(pg)
        if r[0] and r[1] and len(r[0]) >= 6
    ]
    gpos: Counter = Counter(
        grid._row_key(l, f) for l, f in geom
    )

    total = checkable = 0
    reasons: Counter = Counter()
    for t in soup.find_all("table"):
        parsed = grid._parse_html_rows(t)
        if not grid._is_statement_table(parsed):
            continue
        rows = [(l, f) for l, f in parsed if l and f]
        hcnt = Counter(l for l, _f in rows)
        keys = Counter(grid._row_key(l, f) for l, f in rows)
        for l, f in rows:
            if grid._is_period_header(l, f):
                continue  # a column caption, owned by the date check
            total += 1
            key = grid._row_key(l, f)
            by_label = (
                len(l) >= grid.ROW_ORDER_MIN_LABEL
                and hcnt[l] == 1
                and len(pdf_pos.get(l, [])) == 1
            )
            by_key = bool(geom) and len(l) >= 6 and keys[key] == 1 and gpos[key] == 1
            if by_label or by_key:
                checkable += 1
                continue
            # not order-checked at all — record WHY, most specific first
            if len(l) < grid.ROW_ORDER_MIN_LABEL:
                reasons["label too short to locate"] += 1
            elif not geom:
                reasons["PDF has no readable text layer"] += 1
            elif keys[key] > 1:
                reasons["identical row appears twice in the HTML table"] += 1
            elif gpos[key] == 0:
                reasons["PDF row does not match (wrapped label / marker / split)"] += 1
            elif gpos[key] > 1:
                reasons["same row matches several PDF places"] += 1
            elif hcnt[l] > 1:
                reasons["label repeats and values did not match either"] += 1
            else:
                reasons["label not uniquely locatable in the PDF"] += 1
    return total, checkable, reasons


#: (pdfs, html) pairs, grouped as they were filed
BATCHES: list[tuple[list[str], str]] = []


def _load_batches(root: str) -> None:
    import glob
    import os
    from collections import defaultdict

    groups: dict[int, dict[str, list[str]]] = defaultdict(
        lambda: {"pdf": [], "html": []}
    )
    for f in glob.glob(os.path.join(root, "*")):
        if f.endswith(".jpg") or "checked" in f:
            continue
        k = int(os.path.getmtime(f)) // 60
        if f.endswith(".pdf"):
            groups[k]["pdf"].append(f)
        elif f.endswith((".htm", ".html")):
            groups[k]["html"].append(f)
    for k in sorted(groups):
        g = groups[k]
        if not (g["pdf"] and g["html"]):
            continue
        for h in g["html"]:
            BATCHES.append((sorted(g["pdf"]), h))


def main(argv: list[str]) -> int:
    import os

    pairs: list[tuple[list[str], str]]
    if len(argv) >= 2:
        pairs = [(argv[:-1], argv[-1])]
    else:
        root = os.environ.get(
            "SECVERIFY_SWEEP_DIR",
            "/root/.claude/uploads/46640baf-33b1-5345-b567-22d7db67a758",
        )
        _load_batches(root)
        pairs = BATCHES
        if not pairs:
            print(f"no pairs found under {root}")
            return 2

    agg: Counter = Counter()
    tot_rows = tot_ok = 0
    print(f"{'exhibit':46} {'rows':>6} {'checked':>8} {'coverage':>9}")
    for pdfs, html in pairs:
        try:
            n, ok, reasons = analyse(pdfs, html)
        except Exception as exc:  # a broken file must not abort the sweep
            print(f"{os.path.basename(html)[:46]:46} ERROR {type(exc).__name__}: {exc}")
            continue
        if not n:
            print(f"{os.path.basename(html)[:46]:46} {0:>6} {'-':>8} {'no tables':>9}")
            continue
        agg += reasons
        tot_rows += n
        tot_ok += ok
        print(f"{os.path.basename(html)[:46]:46} {n:>6} {ok:>8} {ok / n:>8.0%}")

    print(f"\n{'TOTAL':46} {tot_rows:>6} {tot_ok:>8} "
          f"{(tot_ok / tot_rows if tot_rows else 0):>8.0%}")
    print("\nWhy the remaining rows are order-checked by NOTHING:")
    for reason, n in agg.most_common():
        share = n / tot_rows if tot_rows else 0
        print(f"   {n:6}  {share:5.1%}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
