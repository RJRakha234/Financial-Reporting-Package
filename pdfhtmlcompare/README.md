# pdfhtmlcompare — verify a SEC HTML filing renders the published PDF

At quarter end the financial statements are published as a **PDF** and the *same*
document is converted to **HTML** for the SEC filing. That conversion can
silently transpose a digit, change a label, or drop a whole line out of a table.
`pdfhtmlcompare` reads both documents and checks that the **financial tables**
render the same:

1. **Numbers** — every figure in a financial row matches the HTML.
2. **Wordings** — every row label matches the HTML.
3. **Table lines** — no line item inside a financial table is missing from (or
   invented in) the HTML.

It deliberately **ignores formatting** — layout, ordering, the table of contents,
page headers/footers, signatures, hyphenation, whitespace — none of which is
statement data.

It is a self-contained project with **no dependency on any other project**, and
it runs **entirely offline** (PDF read with `pdfplumber`, HTML with the Python
standard library, annotation with `PyMuPDF`; no network calls).

## Install

```bash
pip install -r requirements.txt
```

## Use it

```bash
# Make a tiny demo PDF + HTML (with planted errors) to try it on
python make_sample.py
python make_sample_html.py

# Compare — writes sample_statement.validated.pdf and sample_filing.commented.html
python -m pdfhtmlcompare sample_statement.pdf sample_filing.html

# Choose paths, skip an output, or print JSON
python -m pdfhtmlcompare published.pdf filed.html -o validated.pdf --out-html commented.html
python -m pdfhtmlcompare published.pdf filed.html --out-html none --json
```

## Two outputs

**1. A validated PDF** — a copy of the published PDF in which every figure that
was checked against the HTML is highlighted **green**, and each discrepancy is
coloured with a sticky-note comment (anchored to its page). A summary page is
prepended.

* **green** — validated (matches the HTML);
* **red, boxed** — a number changed in the HTML;
* **orange** — a number missing from the HTML row, or a whole table line dropped
  (boxed);
* **amber** — the row label wording differs.

**2. A commented HTML** — a copy of the filed HTML with an inline **✓** on every
financial row that matches the PDF and a **✗ / ⚠** note on every row that does
not. Non-financial content is left unmarked.

```
✗ Found 4 finding(s) in the financial tables:

  1. Page 1  ·  [number changed]
     Number differs: PDF shows 8,750, HTML shows 8,570.  ·  cash and cash equivalents

  2. Page 1  ·  [table line missing from HTML]
     Table line missing from HTML (present in the PDF): Goodwill 1,200

  3. Page 1  ·  [wording differs]
     Wording differs: PDF "Trade receivables" vs HTML "Trade receivable".

  4. Page —  ·  [table line only in HTML]
     Table line only in HTML (not in the PDF): Prepaid expenses 250

Rows validated: 7. Figures matched: 8 of 10.
```

The process exits `1` when any finding is reported, `0` when the tables match.

## Library

```python
from pdfhtmlcompare import compare

result = compare("published.pdf", "filed.html",
                 output_pdf="validated.pdf", output_html="commented.html")
print(result.consistent, result.validated_rows)
for f in result.findings:
    print(f.page_label, f.kind, f.message())
```

## How it works

A **financial line** is a short labelled row that carries at least one *real*
figure. Numeric noise is excluded so it does not masquerade as data: four-digit
years, identifier numbers with leading zeros (DINs, membership/registration
numbers), clause/note references like `2.5`, footnote markers like `(1)`, date
days (`June 30`), percentages, and dotted table-of-contents leaders.

1. **Read** both documents into logical lines, keeping each PDF figure's page and
   bounding box (`pdfdoc.py`, `htmldoc.py`).
2. **Align** the financial lines of the two documents by their label
   (`compare.py`). A financial line with no counterpart is a line missing from
   (or added to) a table; displaced-but-identical rows are reconciled rather than
   double-reported.
3. **Compare** the figures and the label wording within each matched row, pairing
   figures by nearest value so a changed number lines up with its own column.
4. **Render** the validated PDF (`annotate_pdf.py`) and commented HTML
   (`annotate_html.py`).

## Scope & limitations

The figure / wording / line checks on the **primary statements** (balance sheet,
P&L, cash flows, statement of changes in equity) are the reliable core —
validated on a real 41-page Ind AS consolidated filing where every primary
figure reconciled to the HTML and no number was found changed. The **notes** are
best-effort: heavily cross-tabulated matrices (e.g. fair-value hierarchy,
financial instruments by category) tokenise columns differently in PDF and HTML,
and a figure embedded in a note *sentence* is not a table row — treat any flag
there as "review this", not a definitive error. Works on text-based PDFs;
scanned/image PDFs would need OCR first.

## Tests

```bash
python -m pytest -q
```
