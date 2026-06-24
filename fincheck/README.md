# fincheck — financial total & subtotal consistency checker

`fincheck` reads a financial-statement PDF, verifies that **every total and
subtotal actually foots** (i.e. equals the sum of its line items), and writes a
copy of the PDF with the result **colour-highlighted** so you can see exactly
what was checked:

* **yellow** — every figure that was summed into a total/subtotal (so you can
  confirm *all* the numbers involved were covered);
* **green** — a total/subtotal that foots correctly;
* **red** — a total that does **not** foot (outlined, with a note);
* **orange** — a total whose components could not be isolated (review manually).

A summary page with a legend, a coverage count ("checked N totals covering M
figures"), and the list of issues is prepended.

It is built for real-world statements: figures with thousands separators,
parenthesised negatives, currency symbols/codes, nil dashes, and nested
balance-sheet / income-statement hierarchies.

## Install

```bash
pip install -r requirements.txt
```

## Use it (command line)

```bash
# Generate a sample statement with two deliberate footing errors
python make_sample.py

# Check it — writes sample_financials.highlighted.pdf next to the input
python -m fincheck sample_financials.pdf

# Choose the output path, or skip the PDF and just print a report
python -m fincheck statements.pdf -o flagged.pdf
python -m fincheck statements.pdf -o none --json
```

Example output:

```
✗ Found 2 total/subtotal inconsistencies:

  1. Page 1  ·  Total current assets
     stated             19,050
     expected           18,950   (off by 100)
     = Inventories 6,800  +  Trade receivables 3,400  +  Cash and cash equivalents 8,750

  2. Page 1  ·  Total expenses
     stated             18,000
     expected           18,250   (off by -250)
     = Cost of materials consumed 11,200  +  ...

Highlighted PDF written to: sample_financials.highlighted.pdf
```

The process exits with status `1` when inconsistencies are found and `0` when
everything foots — handy in CI or a pipeline.

## Use it (library)

```python
from fincheck import analyze

result = analyze("statements.pdf", output_pdf="flagged.pdf")
print(result.consistent)          # False
for issue in result.issues:
    print(issue.label, issue.stated, "expected", issue.expected)
print(result.as_json())
```

## Casting — current vs prior period (interim statements)

`fincheck` also **casts** an interim statement: it checks that, in the current
period, every **year-to-date (six-month) figure equals the current quarter
(three-month) figure plus the same line item's three-month figure from the prior
interim statement** — for both the current year and the comparative year shown:

```
six months ended 30-Sep-2025  ==  three months ended 30-Sep-2025  (current PDF)
                                 + three months ended 30-Jun-2025  (prior PDF)
```

You give it the two PDFs — the current statement (which carries both a
three-month and a six-month column) and the prior statement (which carries the
earlier three-month column):

```bash
# Console summary + Excel + HTML + highlighted PDF (defaults next to the input)
python -m fincheck.cast current_q2.pdf prior_q1.pdf

# Choose outputs explicitly; 'none' skips one
python -m fincheck.cast --current q2.pdf --prior q1.pdf -o casting.xlsx \
    --html casting.html --pdf flagged.pdf
python -m fincheck.cast q2.pdf q1.pdf -o casting.xlsx --pdf none --json

# ±1 rounding drift passes by default; use 0 for a strict, exact cast
python -m fincheck.cast q2.pdf q1.pdf --tolerance 0
```

Example output:

```
✗ 2 figure(s) do not cast

Cast 249 additive figures (tolerance ±1): 247 OK, 2 mismatch; 8 per-share/
share-count figures not cast.

Mismatches (year-to-date ≠ current 3M + prior 3M):
  1. Note 2.24 · Total operating expenses  [2025]
       6-month          8,587
       3M(cur)          4,337  + 3M(prior) 4,252  = 8,589   (off by -2)
```

It produces four things:

* an **Excel workbook** (`Summary` + `Casting` sheets) listing every line item
  with its six-month, current-quarter and prior-quarter figures, the expected
  sum, the exact difference and a colour-coded status — filterable and sortable;
* an **HTML report** that shows the working — each row laid out as
  `year-to-date (6M) = current 3M + prior 3M = expected`, grouped by note and
  colour-coded, so a reviewer can see *how* every figure was cast;
* a **highlighted copy of the current PDF** — every figure that casts is
  **green** (both the six-month total and the current-quarter figure feeding
  it), a six-month figure that does **not** cast is **red** (outlined, with the
  expected value in a note), and anything unverified is **orange**;
* a non-zero **exit code** when anything fails to cast (handy in a pipeline).

Library use:

```python
from fincheck import cast
from fincheck.casting_report import write_excel
from fincheck.casting_html import write_html
from fincheck.casting_highlight import write_highlighted_pdf

result = cast("current_q2.pdf", "prior_q1.pdf", tolerance=1.0)
print(result.consistent, len(result.mismatches))
write_excel(result, "casting.xlsx")
write_html(result, "casting.html")
write_highlighted_pdf(result, "flagged.pdf")
```

**How the line items are matched.** Column geometry is learned *per table* from
that table's own `Three months ended` / `Six months ended` header and year row
(so stray figures elsewhere on the page can't shift the columns), tables are
paired across the two PDFs by the line-item labels they share, and rows are
matched by label with a positional fallback for labels that wrapped or were
dropped in one PDF. Per-share amounts and weighted-average share counts are
detected and **not** cast (they are averages, not additive flows).

**Segment reporting (note 2.23)** uses a different two-line layout — the current
year and the comparative year on separate rows, one column per business segment
plus a Total — so it has a dedicated parser (`fincheck.segment`) that casts every
segment cell across the three-month and six-month matrices; segment names are
recovered best-effort from the wrapped header.

## Offline & data privacy

**fincheck runs entirely offline. It makes no network calls of any kind.** It
only uses local libraries (`pdfplumber`/`pdfminer` to read text, `PyMuPDF` to
annotate, `openpyxl` to write the casting workbook). Your financial statements
are read from disk and the outputs are written back to disk — nothing is
uploaded, sent to any API, logged remotely,
or cached anywhere outside the folder you run it in. It is safe to run on an
air-gapped machine. (You can verify: there is no `requests`/`urllib`/`http`/
`socket`/API-client import anywhere in `fincheck/`.)

## What it covers (validated on a real 63-page IFRS filing)

Tested against a full consolidated IFRS filing (balance sheet, statement of
comprehensive income, cash flows, and ~50 pages of notes):

- **Balance sheet** — every total and subtotal reconciles (Total current assets,
  Total assets, Total liabilities, Total equity attributable + NCI = Total
  equity, Total liabilities and equity), across both reporting periods. ✓
- **Statement of comprehensive income** — Total operating expenses, Total other
  comprehensive income (including unlabelled gross/tax/net subgroupings), and
  Total comprehensive income reconcile across all four periods; derived figures
  (Gross profit, Operating profit, Net profit) are correctly *not* treated as
  additive totals. ✓
- **Cash flows & most note tables** (other income, provisions, etc.) reconcile. ✓

It is robust to the things that break naive checkers: multi-period columns,
date/period header rows, currency symbols, parenthesised negatives, nil dashes,
nested grand totals, and a single misstated subtotal cascading.

### Known limitations

Some heavily **cross-tabulated note matrices** (e.g. *financial instruments by
category* with carrying-value vs fair-value columns, segment reporting, ESOP
grant grids) can mis-align when inline footnote markers shift a figure into a
neighbouring column. These are note disclosures, not the primary statements.
Reliable validation of those generally needs the structured XBRL calculation
linkbase rather than PDF geometry. Treat any flag in such a table as "review
this", not a definitive error.

## How it works

1. **Extract** (`extract.py`) — instead of relying on ruling lines, words are
   clustered into rows by vertical position, numbers are detected and grouped
   into columns by their right edge (financial figures are right-aligned), and
   every cell keeps its bounding box. Label indentation is mapped to hierarchy
   levels.
2. **Check** (`checks.py`) — for each numeric column, every total is reconciled
   **backwards**: walk the pending amounts most-recent-first and stop at the
   first trailing group that sums to the stated total (within a rounding
   tolerance). The matched group is rolled up into a single subtotal, so a
   higher-level total sums subtotals rather than raw items, and one misstated
   subtotal does not cascade. Derived figures (operating profit, net profit, …)
   act as boundaries; unlabelled gross/tax/net subtotals are detected
   implicitly; and a total is only flagged when *no* trailing group reconciles.
3. **Highlight** (`highlight.py`) — PyMuPDF shares the PDF coordinate system, so
   each cell is colour-highlighted by role (yellow component / green total / red
   error / orange unverified). Every figure summed into a total is marked, so
   coverage is visible; failing totals also get an outline and a sticky note,
   and a summary page with a legend and coverage count is prepended. Pass
   `--no-components` (CLI) or `show_components=False` (API) to mark only totals.

## Scope & assumptions

- Checks **additive** totals/subtotals. Derived figures such as *Profit for the
  year* (income − expenses) are intentionally not treated as additive totals, so
  they are left alone.
- A row counts as a total when its label contains *total*, *subtotal*,
  *aggregate*, *sum of*, or *grand total*.
- `--tolerance` sets the absolute rounding slack (default `1.0`); the checker
  also allows ±0.5 per summed component for rounding drift.
- Works on text-based PDFs. Scanned/image PDFs would need OCR first.
```
