# fincheck — financial statement checks

`fincheck` does two jobs on financial statements:

1. **`check`** — foot every total/subtotal in a PDF (see below).
2. **`compare`** — compare a **published PDF** against the **HTML filed with the
   SEC** and comment every difference, page by page, on a copy of the PDF.
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
silently transpose a digit, drop a line, lose a minus sign, or change a word.
`fincheck compare` reads both documents, aligns them, and **comments every
difference on a copy of the published PDF, with a page reference** — so a
reviewer opens the PDF at the flagged page and checks.

```bash
# Generate the sample PDF and a matching HTML filing (with planted errors)
python make_sample.py
python make_sample_html.py

# Compare them — writes sample_financials.compared.pdf next to the PDF
python -m fincheck compare sample_financials.pdf sample_filing.html

# Choose outputs, add a standalone HTML report, or skip the PDF
python -m fincheck compare published.pdf filed.html -o flagged.pdf --html-report diff.html
python -m fincheck compare published.pdf filed.html -o none --json
python -m fincheck compare published.pdf filed.html --no-text   # figures only
```

What it reports, each anchored to the PDF page it came from:

* **figure changed** (red, boxed) — same line, different number in the HTML
  (e.g. PDF `8,750` → HTML `8,570`);
* **figure missing from HTML** (orange) — a figure published in the PDF that the
  HTML does not have in the matching place;
* **figure only in HTML** (blue) — a figure the conversion introduced that is not
  in the PDF;
* **wording differs** (amber) — a word that changed between the two documents.

```
✗ Found 3 differences (3 figure, 0 wording):

  1. Page 1  ·  [figure missing from HTML]
     Figure 1,200 is in the published PDF but missing from the HTML here.  ·  Goodwill

  2. Page 1  ·  [figure changed in HTML]
     Figure mismatch: PDF shows 8,750, HTML shows 8,570.  ·  Cash and cash equivalents

  3. Page 1  ·  [figure only in HTML (not in PDF)]
     Figure 250 appears in the HTML but not in the published PDF here.  ·  Prepaid expenses

Annotated PDF written to: sample_financials.compared.pdf
```

The annotated PDF highlights each discrepancy **in place** on the published
document and attaches a sticky-note comment; a summary page listing every
finding (with its page number) is prepended. The process exits `1` when any
difference is found, `0` when the documents match.

How the alignment works: figures and words are read from both documents in
reading order and matched with a sequence aligner, so only the stretches that
fail to line up are reported — matched figures are never flagged. Within a
mismatched run, figures are paired by their line label first and nearest value
second, so a transposed digit lines up with its own line while a genuinely
added/removed figure is left over and reported as such.

```python
from fincheck import compare

result = compare("published.pdf", "filed.html", output_pdf="flagged.pdf")
print(result.consistent)                 # False
for d in result.differences:
    print(d.page_label, d.kind, d.message())
```

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
