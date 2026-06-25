#!/usr/bin/env python3
"""Generate a sample SEC-style HTML filing from the sample PDF's statements.

This mirrors ``sample_financials.pdf`` (see ``make_sample.py``) but bakes in the
kinds of error a real PDF→HTML conversion introduces, so ``fincheck compare``
has something to catch:

  * Cash and cash equivalents is **8,570** instead of 8,750  — a transposed
    digit (reported as a changed figure);
  * the **Goodwill 1,200** line is dropped                    — missing in HTML;
  * a **Prepaid expenses 250** line is invented               — only in HTML;
  * the company name reads "Acme Manufacturers Ltd"           — wording differs.

Everything else matches the PDF.
"""

from pathlib import Path

OUT = Path(__file__).parent / "sample_filing.html"

# (label, value_or_None) — value None means a heading row with no figure.
BALANCE_SHEET = [
    ("ASSETS", None),
    ("Non-current assets", None),
    ("Property, plant and equipment", "12,450"),
    # ("Goodwill", "1,200"),  <-- deliberately dropped from the HTML
    ("Intangible assets", "800"),
    ("Total non-current assets", "14,450"),
    ("Current assets", None),
    ("Inventories", "6,800"),
    ("Trade receivables", "3,400"),
    ("Prepaid expenses", "250"),  # <-- invented; not in the PDF
    ("Cash and cash equivalents", "8,570"),  # <-- transposed: PDF says 8,750
    ("Total current assets", "19,050"),
    ("Total assets", "33,400"),
]

INCOME = [
    ("Revenue from operations", "24,800"),
    ("Other income", "450"),
    ("Total income", "25,250"),
    ("Expenses", None),
    ("Cost of materials consumed", "11,200"),
    ("Employee benefit expense", "2,150"),
    ("Finance costs", "600"),
    ("Depreciation and amortisation", "1,300"),
    ("Other expenses", "3,000"),
    ("Total expenses", "18,000"),
    ("Profit for the year", "7,000"),
]


def _table(rows):
    out = ['<table border="1" cellspacing="0" cellpadding="4">']
    for label, value in rows:
        cell = value if value is not None else ""
        out.append(
            f"  <tr><td>{label}</td>"
            f'<td align="right">{cell}</td></tr>'
        )
    out.append("</table>")
    return "\n".join(out)


def main():
    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Acme Manufacturers Ltd — Form 10-Q</title></head>
<body>
<h1>Acme Manufacturers Ltd</h1>
<h2>Balance Sheet as at 31 December 2024 (INR '000)</h2>
{_table(BALANCE_SHEET)}
<h2>Statement of Profit and Loss (INR '000)</h2>
{_table(INCOME)}
</body>
</html>
"""
    OUT.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
