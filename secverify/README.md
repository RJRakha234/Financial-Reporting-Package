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
* **brown — “possible issues (lower confidence)”** — a separate, opt-in summary
  bucket for checks *not* held to the zero-false-alarm bar of the red list
  (e.g. a wrong value or transposed column inside a wide movement matrix). It
  surfaces classes the strict checks stay silent on, at the cost of the
  occasional false alarm — so the red list stays trustworthy and this is
  scanned when time allows;
* **blue figure / blue phrase** *(only with `--review-zones`)* — a
  **manual-review zone**, not an error: a financial figure sitting in prose, a
  note/schedule cross-reference, an **in-table comparative row whose exact
  `(label, figures)` the tool could not confirm as a whole against the PDF**, or
  a **repeated-label row whose value disagrees with the PDF occurrence that
  shares its surroundings** (each HTML table is pinned to a PDF region by the
  rows unique on both sides, so "Government securities" under *current* vs
  *non-current* investments is disambiguated by its neighbours — catching an
  *exchange* swap that every whole-row check passes because both values still
  exist against the label; and where a table is a mirrored segment/hierarchy
  schedule with no distinctive neighbour to anchor on, its repeated rows are
  marked wholesale, since the tool cannot confirm which occurrence is which).
  The engine validates figures by presence but cannot always verify their
  *placement* or a reference's *target*, so a token-preserving error (Class 2/3
  — a value swapped between two items where the correct pairing also appears in
  a note, or "refer Note 12" changed to "Note 21") would pass silently. Blue
  says "check this by eye." Rows the tool confirmed exactly stay green;
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
Every report opens with an **Assurance & scope panel** that states what the
tool actually machine-verified — the share of figure-rows fully value-checked,
whether figure signs reconciled document-wide, and any PDF pages too sparse to
read (possible scans) — alongside the honest out-of-scope caveats. It turns the
tool's own coverage into a signable statement rather than an implied guarantee.

The structural detectors run:

* PDF lines whose words are nowhere in the HTML;
* significant PDF figures that never appear in the HTML;
* **row value integrity** — every PDF row's figures must appear beside the
  corresponding occurrence of that row's label in the HTML (window truncated
  at the next row), matched as an **ordered, signed, currency- and
  %-aware** sequence. This one check catches: figures **swapped between line
  items** (both still exist, so presence/count pass); the two **comparative
  period columns transposed** (same figures, wrong order); a **negative shown
  as positive** or vice versa (parentheses/minus stripped by text
  canonicalisation); a **₹↔$ currency swap**; and a **gained or lost %**.
  Section/note numbers (`2.15`), identifiers (membership/UDIN numbers),
  and figures-first movement lines are excluded so they cannot misfire;
* **sign census** — the number of negative occurrences of every significant
  magnitude is reconciled between PDF and HTML document-wide, so a negative
  shown as positive (or vice versa) — e.g. `Total equity 84,643` → `(84,643)`
  — is caught **regardless of the row's label**, closing the gap the row
  check leaves on short-labelled rows;
* **unit of scale** — every "in ₹ crore / million / lakh" declaration the
  PDF makes must appear in the HTML; a silently rescaled table (digits
  unchanged, unit word altered) is flagged;
* **segment / row sequence (geometry)** — even when every figure is present
  and correct, two rows can be **swapped** inside a statement (Life Sciences ↔
  Hi-Tech) with no value check firing. From PDF word geometry the tool pins
  each row to its position and checks the sequence, keyed on `(label, values)`
  so a segment listed twice (revenue **and** profit) is disambiguated. It also
  closes the **transposed matrix** case: where the PDF prints the segment
  schedule as a **wide row** (segments across the columns) but the HTML lists
  each segment as its own row, an HTML column that is an exact permutation of
  a wide PDF row — same figures, different order — is flagged as a **review**
  item. Both require an exact value match before flagging, so a correctly
  ordered table never misfires;
* **content ordering** — distinctive PDF lines that occur exactly once in
  both documents act as sequence anchors; their HTML positions must be
  increasing (per source PDF, via longest-increasing-subsequence). A
  section moved during conversion — page 12's content pasted before page
  1's — is flagged as an out-of-sequence error naming both locations, so
  presence checks can never be satisfied by a scrambled document.
  Smaller relocations get their own tier: a digit-light **prose** anchor out
  of sequence by a sentence-plus but within the error check's slack — a
  paragraph whose every word is verbatim, sitting in the wrong place — is
  reported as a **review** item ("relocated paragraph"), since re-ordering
  otherwise-verbatim blocks is the one silent way to change a document's
  reading order. Table rows are excluded (digit-heavy), so print-vs-web cell
  jitter cannot flood this.

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

# maximum review (toolsigma default): every number green, blue or red — none
# unreviewed. Foot the HTML first, then --footed drops the single-suspect-row
# marks that a tying column sum already covers (permutations stay marked,
# because a sum-preserving swap survives footing). --no-strict turns it off.
python -m secverify.toolsigma statement.pdf auditorsreport.pdf filing.html --footed
```

Exits `1` when inconsistencies are found, `0` when everything validates —
handy in CI or a release checklist.

## Capability levels (base · alpha · beta · sigma)

Every check above runs at the default **base** level. Three cumulative tiers
add further checks on the *same* engine — each is a superset of the one before,
so a higher tier finds everything a lower one does, plus more. Select a tier
with `--level`, or run the named entry point:

| Tier | Adds | Command |
| --- | --- | --- |
| **base** | all core text / figure / row-value / sign / order checks | `python -m secverify …` |
| **alpha** | + **reporting-period date header** (Phase 1) — a current or comparative period date in the HTML that appears nowhere in the PDF is flagged; historical narrative dates are ignored so they never misfire | `python -m secverify.toolalpha …` |
| **beta** | + **table-grid cell comparison** (Phase 2) — the PDF grid is rebuilt from word geometry and compared cell-by-cell against `<table>` rows, catching a **wrong value that exists elsewhere** (so presence passes), a **column transpose**, and a **wrong value on a repeated-label row** (a line item's current/non-current portions, a "total" in several schedules) — each flagged only when the figures appear against that label nowhere in the PDF. Primary-statement rows (2 periods) are hard **red**; **wide movement matrices** (changes in equity, PP&E, 4+ columns) surface in the **brown lower-confidence bucket** | `python -m secverify.toolbeta …` |
| **sigma** | + **hidden text & scanned-page OCR** (Phase 3), and **geometry-bound identifier↔name association** — HTML text present in the DOM but rendered invisible (`display:none`, `visibility:hidden`, off-screen) is surfaced; PDF pages too sparse to extract are OCR-read or reported as un-checkable; and a statutory identifier (a director's DIN, a partner's membership/UDIN) bound to the **wrong person** is caught | `python -m secverify.toolsigma …` |

Each tier is held to the same **zero-false-positive** bar: on all reference
filings tested, alpha, beta and sigma add **no** issues to a correct document —
they only speak when they have a concrete discrepancy to report.

**Identifier↔name association** (B10) is done in sigma by **word geometry**, not
text. Signature blocks are columnar — each signatory is a vertical column of
name / title / DIN, and the columns sit side by side — so flattening the PDF to
reading order collapses them and binds a DIN to the wrong nearest name. Sigma
instead ties each identifier to the name whose *column* (x-position) it sits
under, reconstructing the true pairing; the HTML side keeps preceding-name
proximity (a single linear flow, so the name before a DIN is the right one). A
mismatch is reported only when both sides bind the number to a real name and
those names share no word — so column bleed can only make the check more
conservative, never raise a false positive.

One class is still left to human review: a **pixel render-diff** of visual
formatting (indentation, bold, ruling lines). A paginated PDF and a single-flow
HTML never align page-to-page, so a pixel diff would be dominated by false
positives — that is honestly deferred rather than shipped as a noise source.

`--level` and the tool names are interchangeable — `toolbeta` is exactly
`--level beta`.

## Use it (library)

```python
from secverify import verify

result = verify("statement.pdf", "exv99w09.html", output_html="reviewed.html",
                level="beta")  # base | alpha | beta | sigma (default: base)
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
* Figures are checked for presence, occurrence counts, and — beside the
  right label occurrence — value, order, sign, currency and %. What remains
  outside scope: **totals/subtotals are not re-footed** (that is `fincheck`,
  the sister tool in this repo — run both), and value integrity is assessed
  for label-led rows, so a swap between two adjacent figures-first movement
  lines is left to presence/count and to `fincheck`'s footing.
* Works on text-based PDFs. Scanned/image PDFs need OCR first.
