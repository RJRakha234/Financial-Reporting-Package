# pdfhtmlcompare — verify a SEC HTML filing renders the published PDF

At quarter end the financial statements are published as a **PDF** and the *same*
document is converted to **HTML** for the SEC filing. That conversion can
silently transpose a digit, change a word, or drop a line. `pdfhtmlcompare`
checks that the **whole content** of the PDF — every word *and* every number,
not just the figures — is reproduced in the HTML, and reports whatever is
**changed, missing, or added**, each anchored to the PDF page it came from.

It is a self-contained project with **no dependency on any other project**, and
runs **entirely offline** (PDF read with `pdfplumber`, HTML with the Python
standard library, annotation with `PyMuPDF`; no network calls).

## How it works

Both documents are reduced to an ordered **stream of tokens** — every word and
every number, in reading order — and the two streams are aligned. Comparing
streams (rather than lines) makes the result immune to the fact that the PDF
wraps text to the page while the HTML keeps a paragraph per block: only genuine
content differences surface. Numbers are compared **by value**, so formatting
(commas, leading zeros, a `₹` symbol) never matters but `8,750 → 8,570` does.
Words are folded for case, curly-vs-straight quotes, and hyphen-vs-space, so
cosmetic conversion artefacts are not reported.

The only things excluded are pagination furniture that is not document content:
running page headers/footers (the same line repeated across many pages), lone
page numbers, footnote markers like `(1)`, and date days (`June 30`). Everything
else — statements, notes, the auditor's report, headings, prose — is compared.

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

If the published document spans **several PDFs** (e.g. the auditor's report and
the financial statements) that together correspond to one filed HTML, pass them
all, in order, before the HTML; they are read continuously with page numbers
running across them:

```bash
python -m pdfhtmlcompare auditorsreport.pdf finstatement.pdf filed.html
```

## Two outputs

**1. A validated PDF** — a copy of the published PDF in which every line whose
words and numbers were all found in the HTML is highlighted **green**; a token
that differs is boxed **red** and a token missing from the HTML is **orange**,
each with a sticky-note comment. A summary page with the coverage figure and the
list of findings is prepended.

**2. A commented HTML** — a copy of the filed HTML with an inline **✓** on every
line whose content was found in the PDF and a **✗ / ⚠** note where something
differs, was added, or is missing.

```
✗ Found 4 difference(s):

  1. Page 1  ·  [changed]
     Number differs: PDF shows 8,750, HTML shows 8,570.

  2. Page 1  ·  [changed]
     Word differs: PDF "receivables" vs HTML "receivable".

  3. Page 1  ·  [missing from HTML]
     Missing from HTML (present in the PDF): "Goodwill 1,200".

  4. Page 1  ·  [only in HTML]
     Added in HTML (not in the PDF): "Prepaid expenses 250".

Coverage: 45/49 PDF tokens (91.8%) matched the HTML.
```

The process exits `1` when any difference is found, `0` when the whole content
matches.

## Library

```python
from pdfhtmlcompare import compare

result = compare("published.pdf", "filed.html",
                 output_pdf="validated.pdf", output_html="commented.html")
print(result.consistent, f"{result.coverage * 100:.1f}% matched")
for f in result.findings:
    print(f.page_label, f.kind, f.message())
```

## Validated on a real filing

Run against a real 41-page Ind AS consolidated filing (auditor's report +
financial statements vs the combined SEC HTML): **98.6 % of the PDF's ~21,600
words and numbers matched the HTML**, with the entire balance sheet, P&L, cash
flow statement (including its narrative accounting-policy paragraphs) and notes
validated green.

## Scope & limitations

The residual differences on that filing were concentrated in two areas, both
inherent to reading a text PDF rather than logic errors:

- **Rotated/vertical column headers** in cross-tabulated note tables (statement
  of changes in equity, property-plant-equipment, intangibles) extract as
  unreadable fragments, so those header words show as "only in HTML";
- **2-D layout blocks** such as the signature panel (names in columns) read in a
  different token order in the PDF than in the linear HTML.

Treat findings in those areas as "review", not definitive. Works on text-based
PDFs; scanned/image PDFs would need OCR first.

## Tests

```bash
python -m pytest -q
```
