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

The summary panel at the top lists every item to correct with its remark,
triaged into three tiers so real problems are impossible to miss:
**Discrepancies — act on these** (both documents carry the content but it
differs), **Content with no counterpart in this PDF** (e.g. the auditor's
report in an exhibit whose PDF holds only the statements), and **Layout
artifacts** (every word verified on the cited PDF page; only the print
column-wrapping prevented an exact match). Print-index entries are
validated by label, with the page-number column excluded — an unpaginated
HTML carries no page numbers, and extraction garbles them anyway
(dot leaders turn 19 into "1.9").
Because colouring the HTML alone cannot catch an **omission** (content in
the PDF that the HTML dropped), every line of every PDF page is also checked
against the HTML — and anything missing is rendered **inline as a red
callout box at the exact position in the document where the content should
have appeared** (as a table row when the omission belongs inside a table).
Five structural detectors run:

* PDF lines whose words are nowhere in the HTML;
* significant PDF figures that never appear in the HTML;
* **occurrence counting** — content that appears, say, 2× in the PDF but
  only 1× in the HTML is flagged, so dropping one instance of a repeated
  row (its label and figures also live in a note) is still caught. The same
  counting runs per significant figure;
* **row integrity** — every PDF row's figures must appear beside the
  corresponding occurrence of that row's label in the HTML (window truncated
  at the next row). Two line items whose values were interchanged both still
  pass presence and count checks — this is the check that catches the swap,
  quoting the PDF row against what the HTML shows next to the label;
* **content ordering** — distinctive PDF lines that occur exactly once in
  both documents act as sequence anchors; their HTML positions must be
  increasing (per source PDF, via longest-increasing-subsequence). A
  section moved during conversion — page 12's content pasted before page
  1's — is flagged as an out-of-sequence error naming both locations, so
  presence checks can never be satisfied by a scrambled document.

## Install

```bash
pip install -r requirements.txt
```

## Use it (command line)

```bash
# writes exv99w09.checked.html next to the input
python -m secverify statement.pdf exv99w09.html

# several reference PDFs: the exhibit combines the statements AND the signed
# auditor's report — validate against both (remarks cite "auditorsreport p.2")
python -m secverify statement.pdf auditorsreport.pdf exv99w09.html

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
   tiers), and occurrence counts are compared for repeated content. Missing
   lines and count shortfalls join the numbered issue list and are placed
   inline: the callout is anchored to the DOM element reflecting the nearest
   preceding covered PDF line (or the surviving instance, for count
   shortfalls), so the reviewer sees the omission in context. Reflected-but-
   not-verbatim lines are listed compactly in the summary panel.
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

* The PDF(s) are treated as the source of truth; the HTML is what gets
  checked — and the reverse pass verifies the HTML reflects *all* of every
  reference PDF. Pass every source the exhibit is assembled from (statements
  + signed auditor's report), otherwise their sections show as "content with
  no counterpart".
* Figures are checked four ways: presence, occurrence counts, row
  integrity (beside the right label occurrence), and inside exact line
  matches. A swap between two rows inside the same tight window (adjacent
  single-figure rows) can still evade the row check — `fincheck` (sister
  tool in this repo) complements it by verifying totals/subtotals foot
  within each statement, which a swap almost always breaks.
* Works on text-based PDFs. Scanned/image PDFs need OCR first.
