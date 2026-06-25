# fincheck — financial statement checks

`fincheck` does two jobs on financial statements:

1. **`check`** — foot every total/subtotal in a PDF (see below).
2. **`compare`** — compare a **published PDF** against the **HTML filed with the
   SEC**, producing a PDF green-highlighted where validated and an HTML with a
   ✓/✗ comment on every row.
   Jump to [Compare published PDF vs filed HTML](#compare-published-pdf-vs-filed-html).

## `check` — total & subtotal consistency

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

## Compare published PDF vs filed HTML

At quarter end, after the financials are published as a PDF, the **same**
document is converted to **HTML** for the SEC filing. That conversion can
silently transpose a digit, drop a whole table row, lose a minus sign, or change
a word. `fincheck compare` reads both documents, aligns them **row by row**, and
produces **two** outputs:

1. a **validated PDF** — a copy of the published PDF in which every figure that
   was checked against the HTML is highlighted **green**, and each discrepancy is
   marked in colour with a sticky-note comment (anchored to the page it is on);
2. a **commented HTML** — a copy of the filed HTML with an inline **✓** on every
   row that matches the PDF and a **✗ / ⚠** note on every row that does not.

```bash
# Generate the sample PDF and a matching HTML filing (with planted errors)
python make_sample.py
python make_sample_html.py

# Compare them — writes sample_financials.validated.pdf and
# sample_filing.commented.html next to the inputs
python -m fincheck compare sample_financials.pdf sample_filing.html

# Choose paths, add a standalone HTML summary, skip an output, or print JSON
python -m fincheck compare published.pdf filed.html \
    -o validated.pdf --out-html commented.html --html-report summary.html
python -m fincheck compare published.pdf filed.html --out-html none --json
python -m fincheck compare published.pdf filed.html --no-text   # figures only
```

What it reports, each anchored to the PDF page it came from:

* **row dropped in HTML** (orange, boxed) — a whole table line that is in the PDF
  but missing from the HTML (a table-formatting miss);
* **row only in HTML** (blue) — a line the conversion introduced that is not in
  the PDF;
* **figure changed** (red, boxed) — same row, different number (e.g. `8,750` →
  `8,570`);
* **figure missing from HTML / only in HTML** (orange / blue) — a cell dropped
  from or added to an otherwise-matching row;
* **wording differs** (amber) — a word that changed inside a matching row;
* everything that checks out is highlighted **green** (validated).

```
✗ Found 4 differences:
    1 row(s) dropped in HTML, 1 row(s) only in HTML, 1 figure, 1 wording.

  1. Page 1  ·  [row dropped in HTML]
     Row dropped in HTML (present in the PDF): Goodwill 1,200

  2. Page 1  ·  [row only in HTML]
     Row only in HTML (not in the published PDF): Prepaid expenses 250

  3. Page 1  ·  [figure changed in HTML]
     Figure mismatch: PDF shows 8,750, HTML shows 8,570.  ·  Cash and cash equivalents

  4. Page 1  ·  [wording differs]
     Wording differs: PDF "Manufacturing" vs HTML "Manufacturers".  ·  Manufacturing

Rows validated: 23. Figures: 19 matched of 21 in the PDF.
Validated PDF written to: sample_financials.validated.pdf
Commented HTML written to: sample_filing.commented.html
```

The process exits `1` when any difference is found, `0` when the documents match.

How the alignment works — two passes:

1. **Line level.** The logical lines (table rows, headings) of the two documents
   are aligned. A figure-bearing PDF line with no match in the HTML is a *dropped
   row*; the reverse is an *extra row*. Running page headers/footers, index
   (dot-leader) lines, and prose paragraphs are excluded so the row check stays on
   actual table rows; displaced-but-identical rows are reconciled rather than
   double-reported.
2. **Cell level.** Within a matched row, figures and words are aligned, so a
   changed figure, a dropped/added cell, or a changed word is pinpointed. Figures
   are paired by their line label first and nearest value second, so a transposed
   digit lines up with its own line while a genuinely added/removed figure is left
   over and reported as such.

```python
from fincheck import compare

result = compare(
    "published.pdf", "filed.html",
    output_pdf="validated.pdf", output_html="commented.html",
)
print(result.consistent, result.validated_rows)
for d in result.differences:
    print(d.page_label, d.kind, d.message())
```

### Scope & limitations of `compare`

The figure and table-row checks on the **primary statements** (balance sheet,
P&L, cash flows, changes in equity) are the reliable core — validated on a real
41-page Ind AS consolidated filing where every balance-sheet figure in both
periods reconciled to the HTML. Because PDF and HTML break **prose** differently
(the PDF wraps to the page; the HTML keeps one paragraph per block), the row
check is deliberately limited to figure-bearing rows; narrative-heavy **notes**
where figures are embedded in sentences, and heavily cross-tabulated note
matrices, are best-effort — treat a flag there as "review this", not a definitive
error. Works on text-based PDFs; scanned/image PDFs need OCR first.

## Use it (library)

```python
from fincheck import analyze

result = analyze("statements.pdf", output_pdf="flagged.pdf")
print(result.consistent)          # False
for issue in result.issues:
    print(issue.label, issue.stated, "expected", issue.expected)
print(result.as_json())
```

## Offline & data privacy

**fincheck runs entirely offline. It makes no network calls of any kind.** It
only uses local libraries (`pdfplumber`/`pdfminer` to read PDF text, `PyMuPDF` to
annotate, and the Python standard library's `html.parser` to read the filed
HTML — no third-party HTML dependency, no remote fetch). Your financial
statements are read from disk and the annotated PDF is written back to disk —
nothing is uploaded, sent to any API, logged remotely, or cached anywhere
outside the folder you run it in. It is safe to run on an
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
