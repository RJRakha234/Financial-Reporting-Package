# finmatch — compare two financial PDFs by *language*, ignoring the numbers

When you produce the same financial statements in two currencies — e.g. an IFRS
filing in **INR (crores)** and in **USD (millions)** — the wording, line items,
headings and notes must be **identical**; only the figures should differ.
`finmatch` verifies exactly that: it masks out the numbers (and, by default, the
currency symbols and unit words), aligns the two documents by their remaining
language, and **highlights every line green (matched) or red (not matched)** in
a copy of each PDF.

So *"Cash and cash equivalents 6,75,000 7,20,000"* (INR) and *"Cash and cash
equivalents 288.6 238.0"* (USD) come back **green** — same language — while a
renamed line item, a typo, or a line present in one file but not the other comes
back **red**.

## Install

```bash
pip install -r requirements.txt
```

(`finmatch` needs PyMuPDF; `reportlab` is only for the sample generator.)

## Use it (command line)

```bash
# Generate two sample statements (same wording, different currency + 2 planted diffs)
python make_sample.py

# Compare them — writes <input>.compared.pdf next to each input
python -m finmatch balance_sheet_inr.pdf balance_sheet_usd.pdf

# Side-by-side HTML report as well
python -m finmatch balance_sheet_inr.pdf balance_sheet_usd.pdf --html report.html

# JSON for a pipeline; choose output paths; skip the highlighted PDFs
python -m finmatch a.pdf b.pdf --json -o none -O none
```

Example output:

```
✗ Language differences found.
10 lines matched · 3 not matched · 76.9% language match

  1. A  p1: Goodwill
     B  —
  2. A  p1: Trade receivables
     B  p1: Trade and other receivables

Highlighted PDFs written to:
  balance_sheet_inr.compared.pdf
  balance_sheet_usd.compared.pdf
```

Each highlighted PDF gets a **summary page** (legend + match rate) prepended, and
every line on every page is shaded **green** (language matched) or **red** (not
matched). The process exits `1` when differences are found, `0` when the language
is fully consistent — handy in CI.

### Options

| flag | effect |
|------|--------|
| `-o / --output-a`, `-O / --output-b` | output paths (`none` to skip a PDF) |
| `--keep-currency` | do **not** mask currency symbols/unit words, so `INR`/`USD`, `crores`/`millions` are reported as differences |
| `--case-sensitive` | treat letter-case differences as mismatches |
| `--json` | print a JSON report |
| `--html PATH` | write a side-by-side HTML report |
| `--no-color` | plain console output |

## Use it (library)

```python
from finmatch import analyze

result = analyze("balance_sheet_inr.pdf", "balance_sheet_usd.pdf")
print(result.consistent, result.coverage)     # False, 0.769...
for row in result.rows:
    if row.kind == "mismatch":
        print(row.a.text if row.a else "—", "||", row.b.text if row.b else "—")
```

## How it works

1. **Extract** (`extract.py`) — PyMuPDF reads each page's words with bounding
   boxes and groups them into lines. The boxes are reused for highlighting, so no
   coordinate conversion is needed.
2. **Normalise** (`normalize.py`) — each line is reduced to its *language*:
   numbers (with thousands separators in both Western `1,234,567.89` and Indian
   `12,34,567.89` styles, parenthesised negatives, percentages, nil dashes) are
   masked to `#` and collapsed, and currency symbols/unit words are removed by
   default. `"(₹ in crores)"` and `"($ in millions)"` both become `"( in )"`.
3. **Align** (`compare.py`) — the normalised line sequences are aligned with
   `difflib.SequenceMatcher`, which tolerates inserted/removed/reordered lines.
   Each line is labelled `match` or `mismatch`; blank/number-only lines never
   count.
4. **Highlight** (`highlight.py`) — PyMuPDF shades each line green or red and
   prepends a summary page with a legend and the match rate. `report.py` adds
   console, JSON and side-by-side HTML views.

## Scope & limitations

- Works on **text-based** PDFs. Scanned/image PDFs need OCR first.
- Comparison is **line-level**: a real wording change anywhere on a line flags
  the whole line (with the numbers masked out of the comparison).
- It checks **language consistency**, not arithmetic. To check that the numbers
  themselves foot, use the sibling **`fincheck`** tool. To compare with **zero
  installed libraries** (air-gapped box), use the sibling **`pdfcompare`** tool.

## Tests

```bash
python -m pytest
```

`make_sample.py` builds the test fixtures with `reportlab`, so the suite runs
offline once the requirements are installed.
