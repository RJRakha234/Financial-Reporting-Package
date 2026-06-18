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

## Rollforward check (comparatives vs. previously published figures)

A second tool checks that the **prior-period (comparative) figures** in a current
filing match the numbers **as originally published** in earlier filings. For the
quarter ended 30 June 2026, the results also restate the quarter ended 30 June
2025 and the year ended 31 March 2026; those comparatives must equal what was
filed last year. When they don't, something was mis-keyed or silently restated.

It works period-by-period: each numeric column is labelled with its reporting
period (end date + type — quarter/year/as-at), and for every period that appears
in **both** the current filing and a prior one, each line item's comparative is
matched (by period and label) against the published figure. Differences beyond a
rounding tolerance are flagged.

```bash
# Generate two sample filings (June-2026 quarter + the published June-2025 quarter)
# with one deliberate rollforward error (Other income shown as 120, published 95).
python make_rollforward_sample.py

# Check the current filing's comparatives against the prior published filing(s).
# The first PDF is the filing under review; the rest are previously published.
python -m fincheck.rollforward rf_current.pdf rf_prior.pdf

# Multiple priors (e.g. last year's quarter + last year's annual report), JSON,
# or skip the highlighted PDF:
python -m fincheck.rollforward current.pdf prior_q1.pdf annual_report.pdf
python -m fincheck.rollforward current.pdf prior.pdf -o none --json
```

Example output:

```
✗ Found 4 rollforward mismatches (of 12 comparative figures checked):

  1. quarter ended 30 Jun 2025  ·  Other income
     in current filing                120
     as published                      95   (off by 25)
     published in: rf_prior.pdf
  ...

Note: no prior filing supplied covered these comparative periods, so they
could not be checked:
  · year ended 31 Mar 2026
```

It writes `current.rollforward.pdf` highlighting every checked comparative
(green = matches what was published, red = mismatch) and prepends a summary page.
As a library:

```python
from fincheck import check_rollforward

result = check_rollforward("current.pdf", ["prior_q1.pdf", "annual.pdf"])
print(result.consistent)              # False
for c in result.mismatches:
    print(c.current.period.describe(), c.current.label,
          c.current.value, "vs published", c.prior.value)
print(result.as_json())
```

It exits `1` when a mismatch is found and `0` when every comparative ties out.
Period detection is geometry-based and best-effort: a comparative column whose
period (or a prior filing's period) can't be identified is simply skipped, and
any period the current filing reports without a matching prior is listed under
the "could not be checked" note rather than silently passed.

## Offline & data privacy

**fincheck runs entirely offline. It makes no network calls of any kind.** It
only uses local libraries (`pdfplumber`/`pdfminer` to read text, `PyMuPDF` to
annotate). Your financial statements are read from disk and the highlighted PDF
is written back to disk — nothing is uploaded, sent to any API, logged remotely,
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
