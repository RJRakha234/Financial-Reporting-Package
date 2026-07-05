# secverify — validate an SEC-filing HTML against the published PDF

When results are published in India as a PDF and the same document is
converted to HTML for the SEC (EDGAR exhibit), the HTML must be checked
against the PDF before filing. `secverify` verifies that the HTML
**correctly and completely reflects the PDF — all text and all numbers, in
both directions** — offline, and writes a **highlighted HTML review copy**:

* **green figure** — the amount was found in the PDF (hover shows the pages);
* **green block** — the text matches the PDF;
* **amber block** — close match, review manually (e.g. a table header the PDF
  wraps across columns, or wording that differs slightly);
* **red figure / red block** — not found in the PDF. Every red/amber item
  carries a **remark** saying exactly what to correct: a tooltip on hover, a
  `[n]` marker linking to the summary panel at the top, and an HTML comment
  (`<!-- SECVERIFY REMARK #n: … -->`) next to the highlight in the source.

The summary panel at the top lists every item to correct with its remark.
Because colouring the HTML alone cannot catch an **omission** (content in
the PDF that the HTML dropped), the review copy ends with a **PDF → HTML
coverage map**: every line of every PDF page, coloured by whether the HTML
reflects it, with remarks on anything missing. Three omission detectors run:

* PDF lines whose words are nowhere in the HTML;
* significant PDF figures that never appear in the HTML;
* **occurrence counting** — content that appears, say, 2× in the PDF but
  only 1× in the HTML is flagged, so dropping one instance of a repeated
  row (its label and figures also live in a note) is still caught. The same
  counting runs per significant figure.

## Install

```bash
pip install -r requirements.txt
```

## Use it (command line)

```bash
# writes exv99w09.checked.html next to the input
python -m secverify statement.pdf exv99w09.html

# choose the output paths, add a machine-readable report
python -m secverify statement.pdf exv99w09.html -o reviewed.html --json report.json
```

Exits `1` when inconsistencies are found, `0` when everything validates —
handy in CI or a release checklist.

## Use it (library)

```python
from secverify import verify

result = verify("statement.pdf", "exv99w09.html", output_html="reviewed.html")
print(result.figures_ok, "/", result.figures_total, "figures validated")
for issue in result.issues:
    print(f"#{issue.num} [{issue.kind}/{issue.severity}]", issue.remark)
```

## Offline & data privacy

**secverify runs entirely offline. It makes no network calls of any kind.**
It only uses local libraries (`pdfplumber` to read the PDF, `beautifulsoup4`
to parse and annotate the HTML). Financial statements are read from disk and
the annotated copy is written back to disk — nothing is uploaded or logged
anywhere. Safe for unpublished, price-sensitive results on an air-gapped
machine.

## How it works

1. **PDF corpus** (`pdfside.py`) — every page's text and every numeric token
   is indexed. Figures are taken from tightly-tokenised words so adjacent
   table columns can never merge; letter-spaced figures ("3 0 2 8") are
   re-assembled by glueing digit fragments that touch. Running headers,
   footers and page numbers are detected (short lines repeating on ≥40% of
   pages) and dropped so sentences that span a page break still match.
2. **Figures** (`numbers.py`) — handles western (1,234,567) and Indian
   lakh/crore (12,34,567 and the hybrid 415,42,72,628) grouping,
   parenthesised negatives, ₹/$ prefixes, percentages, and trailing-zero
   equivalence (25.30 = 25.3). Comparison is sign-insensitive because the
   same figure may appear as `(240)` in one rendering and `240` under a
   "deductions" heading in the other. Dates written without a space
   ("June 30,2025") are correctly split, not read as 302,025. A figure that
   fails gets a remark listing the *closest* PDF figures (digit-similarity
   first — transpositions and typos rank on top) with their pages.
3. **Text** (`textnorm.py`) — matching is done on a canonical form that keeps
   only alphanumeric characters, because PDF extraction loses whitespace
   unpredictably and the two renderings disagree about quotes/dashes/
   ligatures. Three tiers per sentence: exact match including figures →
   exact match of the words alone (figures are validated separately) →
   fuzzy match with a similarity score. A word-coverage check downgrades
   multi-column table headers (all words present on one PDF page, order
   scrambled by extraction) from error to review.
4. **Coverage** (`coverage.py`) — the reverse direction: every line of every
   PDF page is checked against the HTML's text and figures (same canonical
   tiers), occurrence counts are compared for repeated content, and the
   page-by-page coverage map is appended to the review copy. Missing lines
   and count shortfalls join the numbered issue list.
5. **Annotate** (`annotate.py`) — wraps every figure in a coloured span, tags
   every text block, injects the CSS + summary panel, and records remarks as
   tooltips, `[n]` markers and HTML comments.

## Reading the result

* Content that exists **only in the HTML** (e.g. the Independent Auditor's
  Report and its UDIN, or the "Exhibit 99.9" sheet label, when the source
  PDF contains only the statements) is correctly flagged red — the remark
  says no similar passage exists. Confirm such sections against their own
  source rather than this PDF.
* Amber means "a human should look": most amber items are PDF extraction
  artifacts (cross-column table headers), not real errors — the remark
  points at the PDF page to eyeball.
* The annotated copy is for **review only** — file the original HTML, never
  the highlighted one.

## Scope & assumptions

* The PDF is treated as the source of truth; the HTML is what gets checked —
  and the coverage map verifies the HTML reflects *all* of the PDF.
* Figure matching is presence- and count-based across the whole document; a
  figure swapped between two rows that both exist would not be flagged —
  pair it with `fincheck` (sister tool in this repo), which verifies that
  totals/subtotals foot within a statement.
* Works on text-based PDFs. Scanned/image PDFs need OCR first.
