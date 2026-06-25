#!/usr/bin/env python3
"""Generate a sample SEC-style HTML filing from the sample PDF's statement,
with the kinds of error a real PDF→HTML conversion introduces, so the tool has
something to catch:

  * Cash and cash equivalents is **8,570** instead of 8,750  — number changed;
  * the **Goodwill 1,200** line is dropped                   — table line missing;
  * a **Prepaid expenses 250** line is invented              — line only in HTML;
  * "Trade receivables" reads "Trade receivable"             — wording differs.
"""

from pathlib import Path

OUT = Path(__file__).parent / "sample_filing.html"

ROWS = [
    ("ASSETS", ""),
    ("Non-current assets", ""),
    ("Property, plant and equipment", "12,450"),
    # ("Goodwill", "1,200"),                 <-- dropped from the HTML
    ("Intangible assets", "800"),
    ("Total non-current assets", "14,450"),
    ("Current assets", ""),
    ("Inventories", "6,800"),
    ("Trade receivable", "3,400"),           # <-- wording: PDF says "Trade receivables"
    ("Prepaid expenses", "250"),             # <-- invented; not in the PDF
    ("Cash and cash equivalents", "8,570"),  # <-- changed: PDF says 8,750
    ("Total current assets", "18,950"),
    ("Total assets", "33,400"),
]


def main():
    rows = "\n".join(
        f'  <tr><td>{lbl}</td><td align="right">{val}</td></tr>' for lbl, val in ROWS
    )
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Acme Manufacturing Ltd — Form 10-Q</title></head>
<body>
<h1>Acme Manufacturing Ltd</h1>
<h2>Balance Sheet as at 31 December 2024 (INR '000)</h2>
<table border="1" cellspacing="0" cellpadding="4">
{rows}
</table>
</body></html>
"""
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
