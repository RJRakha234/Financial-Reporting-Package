# fincheck — financial statement checks on PDFs

Two tools in one package:

1. **`fincheck statements.pdf`** — verify that every total and subtotal in a
   financial statement actually foots, and highlight the result.
2. **`fincheck compare old.pdf new.pdf`** — compare two PDFs **exactly**, with
   no layout assumptions at all. See [Comparing two PDFs](#comparing-two-pdfs-exactly).
3. **`fincheck compare a.pdf b.pdf --side-by-side out.html`** — place every
   paragraph and table beside its counterpart, matched by content. This one
   *does* infer structure, and says so. See [Side by side](#side-by-side-every-paragraph-and-table).

## Checking totals

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

## Offline & data privacy

**fincheck runs entirely offline. It makes no network calls of any kind.** It
only uses local libraries (`pdfplumber`/`pdfminer` to read text, `PyMuPDF` to
annotate). Your financial statements are read from disk and the highlighted PDF
is written back to disk — nothing is uploaded, sent to any API, logged remotely,
or cached anywhere outside the folder you run it in. It is safe to run on an
air-gapped machine. (You can verify: there is no `requests`/`urllib`/`http`/
`socket`/API-client import anywhere in `fincheck/`.)

## What the totals checker covers (validated on a real 63-page IFRS filing)

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

## How the totals checker works

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

## Scope & assumptions of the totals checker

- Checks **additive** totals/subtotals. Derived figures such as *Profit for the
  year* (income − expenses) are intentionally not treated as additive totals, so
  they are left alone.
- A row counts as a total when its label contains *total*, *subtotal*,
  *aggregate*, *sum of*, or *grand total*.
- `--tolerance` sets the absolute rounding slack (default `1.0`); the checker
  also allows ±0.5 per summed component for rounding drift.
- Works on text-based PDFs. Scanned/image PDFs would need OCR first.

## Comparing two PDFs exactly

**Can two PDFs be compared exactly, with no assumptions?** For the question
*"are these the same document, and if not, what changed?"* — yes, completely.
For the question *"which line item changed?"* — no, and no tool can, for a
reason that is worth understanding before you trust any of them.

A PDF does not contain a table. It contains drawing operations: *show this
string, in this font, at this point*; *stroke this line*; *paint this image*.
Rows, columns and line items are things a human eye assembles from where those
operations land. Any tool that tells you "Total current assets in the FY25
column changed" has reconstructed a table from coordinates, and that
reconstruction can be wrong — which is exactly the failure mode listed under
[Known limitations](#known-limitations) for the footing checker.

So `fincheck compare` does not reconstruct anything. It compares the drawing
operations themselves, in five independent layers, each an exact equality test:

| Layer | What is compared | Answers |
|---|---|---|
| bytes | SHA-256 of each file | is this literally the same file? |
| geometry | page count, page size, rotation | same shape? |
| text | every character drawn, in baseline order | did any wording or figure change? |
| spans | every string with its font, size, colour and position | did anything move or restyle? |
| pixels | both pages rendered and compared byte for byte | do they *look* the same? |

If all five say "same", the documents are the same — there is nothing left in
a PDF for them to differ by. If any says "differs", it reports precisely what
differs in the file's own units: this string, at this point, became that
string; these pixels in this rectangle changed.

```bash
fincheck compare draft.pdf final.pdf              # writes final.diff.pdf
fincheck compare draft.pdf final.pdf -o none      # report only
fincheck compare draft.pdf final.pdf --json       # machine-readable
```

```
  byte-for-byte identical ............. DIFFERS
  page count and geometry ............. same
  text, every character ............... DIFFERS     (1 page(s))
  text spans, incl. font+position ..... DIFFERS     (1 page(s))
  vector graphics and images .......... DIFFERS     (1 page(s))
  rendered pixels at 150 dpi .......... DIFFERS     (1 page(s), 0.635% of pixels)

  Verdict: the documents are NOT identical (1 of 1 page(s) differ).

Figures that changed (1):

  Page 1  ·  Total current assets
      19,050 -> 18,950   (change of -100)

Differences by page:

  Page 1
    ~ edited   '19,050' -> '18,950'   at (473.656, 325.031)   [Total current assets]
    + added    'Impairment loss'      at (91.0394, 557.047)
    → moved    'Total expenses'       (91.0394, 593.047) -> (91.0394, 611.047)
```

Because the layers are independent, the report distinguishes cases a single
verdict would blur together — a re-export with identical content but different
bytes, a page that reads the same but was re-typeset, a scanned page with no
text layer where only the pixels can speak, and a figure that genuinely
changed.

Exit status is `0` when the documents are identical and `1` when they differ,
so it works as a release gate.

### The one inference, and it is labelled

The `[Total current assets]` in square brackets above is the nearest
non-numeric text sharing that figure's baseline. Sharing a baseline is an
exact fact about the file; reading it as "the same row of the same table" is a
guess. It is printed because it makes the report readable, it is marked as a
reading aid wherever it appears, and it never affects any verdict. Everything
else — the values, the positions, the page — is exact.

### Options

- `--no-render` skips the pixel layer. Faster, but visual equality is then
  reported as *not checked* rather than silently assumed.
- `--dpi N` sets the render resolution (default 150). Pixel equality is a
  statement about a rendering: deterministic and reproducible for a given
  renderer and resolution, which is why the resolution is always printed.
- `--position-tolerance P` quantises coordinates before comparing, for when a
  re-export drifts by a fraction of a point. The default is `0` — exact.
- `--align-pages` matches pages by content hash instead of by position, for a
  document with a page inserted or removed. Off by default, because pairing
  page *n* with page *n* is the only pairing that assumes nothing; the
  alignment is a stated choice you opt into.

### Library use

```python
from fincheck import compare

result = compare("draft.pdf", "final.pdf", output_pdf="diff.pdf")
print(result.identical)            # False
print(result.text_identical)       # did any character change?
print(result.visually_identical)   # None if rendering was skipped

for page, change in result.numeric_changes:
    print(page.page_b + 1, change.before.value, "->", change.after.value, change.delta)
```

### The marked-up PDF

`fincheck compare` writes a copy of the second PDF with the differences marked
in place — green for added text, red boxes where text was removed, amber for
an edit in place (the old string is in the sticky note), blue for text that
only moved or restyled, and a violet outline around any region whose pixels
differ *without* a text change to explain it, which is where a moved rule or a
swapped image shows up. A summary page carrying the five verdicts is
prepended.

A page whose text differs in more than 300 places is left unmarked rather than
marked partially. Past that point the page was re-typeset rather than edited,
every line would carry a highlight, and the mark-up would obscure rather than
show. The summary page says how many pages were skipped, and the console and
JSON reports still list every change.

## The auditor's one-command form

For the review workflow — a **source document** (the benchmark) converted to
HTML and re-exported as PDF, verified back against the source — one command
produces the full deliverable set in a folder:

```bash
python -m fincheck compare --source source.pdf --compared filed.pdf --output ./results/
```

```
Report written to: results/comparison_report.html
Annotated copy written to: results/source_annotated.pdf
Annotated copy written to: results/compared_annotated.pdf

Composite match with benchmark: 97%
  1,135 segments matched (266 passages, 869 table rows); 12 deviate, 0 figure cells deviate.
  3 in the benchmark only, 5 not in the benchmark, 1 section(s) moved.
```

- `comparison_report.html` — self-contained (all CSS/JS inline): side-by-side
  and Word-style tracked-changes views, per-segment 0–100 match scores, a
  section register with page links, filters, and an in-page preview of the
  highlighted source for every cell.
- `source_annotated.pdf` / `compared_annotated.pdf` — each input with every
  compared passage tinted by outcome, sections outlined and stamped with their
  serial from the report, and matching bookmarks. Untinted content was **not**
  covered by the comparison, visibly.

**Scores.** Each segment scores 100 for an exact content match, 99 for a
formatting-only difference (punctuation, bullet glyphs), 50–98 for a partial
match in proportion to the content shared, and 0 for a segment with no
counterpart. The headline number is the length-weighted composite across all
segments. Exit status is non-zero when anything deviates, for CI use.

**Moved content.** Matching is content-based, never page-based; a section
relocated in one document still pairs with its counterpart and is flagged
`moved` in the register rather than reported as a removal plus an addition.

### The independent figure reconciliation

Everything above rests on a similarity judgement, and a judgement can be
wrong: a mis-paired row compares a figure against the wrong counterpart, or
against none. For financial reporting that is not good enough on its own, so
every run also produces a check that owes the alignment nothing:

> Every numeric token on every page of both files, reconciled as two
> multisets. No paragraphs, no rows, no similarity anywhere in the chain.

It cannot tell you a figure moved — only whether one was **lost or invented**,
and how many times each document prints it. Amounts printed in the benchmark
and *nowhere* in the compared document are called out as `absent`; a figure
printed three times in one and twice in the other is reported as exactly that,
not as missing. Values are compared as parsed numbers with no rounding, so
`1,234.50` reconciles with `1,234.5` and never with `1,234.51`.

The report leads with this panel, the console prints it, and `result.ledger`
exposes it (`reconciled`, `coverage`, `absent_amounts`, `only_in_a`,
`only_in_b`). Pass `figure_ledger=False` to skip it.

### Reviewing in the report

The report is a working console, not just a document:

- **Accept / Reject** on every deviating section, with a progress bar over the
  decisions still open, saved in the browser and exportable as **CSV or JSON**.
- **Only undecided** hides everything already settled.
- **Four views** — side by side, tracked changes, before (benchmark as
  written), after (compared as written) — set globally *and* overridable per
  section, because a reviewer reads most of a report one way and one passage
  another.

## Side by side, every paragraph and table

The exact layers above answer *whether* two documents differ. They will not put
a statement next to its counterpart, because doing that requires deciding what a
paragraph and a table are — and a PDF does not say. `--side-by-side` makes that
decision explicitly and shows its work:

```bash
fincheck compare draft.pdf final.pdf --side-by-side review.html
```

```
Side-by-side written to: review.html
  1,135 passages matched by content (266 paragraphs, 869 table rows);
  234 differ, 324 figure cells differ.
  147 only in A, 320 only in B. This view infers paragraphs and tables.
```

**Matched by content, not by page.** When one document sets a statement
landscape over two pages and the other portrait over three, page 4 of one has
nothing to do with page 4 of the other. Matching runs the way a good text diff
does: passages whose content occurs exactly once in each document are
unambiguous anchors and are paired first; the leftovers get an order-preserving
similarity alignment between those anchors. Order preservation matters, because
statements repeat labels — *Total*, *Others*, *Investments* — endlessly.

**The unit of comparison differs by kind, and it has to.** Prose is compared a
paragraph at a time, because the two files wrap lines at different measures and
no line of one corresponds to a line of the other. Tables are compared a row at
a time, because the two files group rows into tables differently — one keeps a
balance sheet whole, the other splits it over three pages — so whole-table
matching collapses on exactly the statements that matter most.

**Wide tables stack rather than scroll.** Past six figure columns, two
side-by-side copies cannot both fit on screen, so A is laid above B with the
columns aligned. Reading down a column beats scrolling sideways to find its
counterpart — and a column the other document lost shows as a row of dashes.

**Columns are named after how each PDF was made.** "The Excel one" and "the HTML
one" is how people actually refer to two renderings of the same statements, so
that is what the column headings say — read from each file's producer metadata,
and shown in a header that stays on screen as you scroll. Override with
`--label-a` / `--label-b`:

```bash
fincheck compare source.pdf filed.pdf --side-by-side review.html \
    --label-a "Source PDF" --label-b "Filed exhibit"
```

The page opens with an index of the sections that differ on both sides, has
filters for hiding identical and one-sided sections, and folds away the wrapped
label lines a narrower page produces (counted, not dropped).

### Numbering the sections yourself

Content matching is a similarity judgement, and a judgement can decline: two
passages that plainly correspond may fall below the floor and be reported as
present in one document only — a blank where a comparison belongs. If you can
see that they correspond, you can say so.

**Highlight the passage in any PDF reader and type a number in the comment.
Put the same number on its counterpart in the other file.** That is all:

```bash
fincheck compare exhibit.pdf original.pdf --side-by-side review.html
#   Honoured 16 section number(s) marked in both files;
#   none of them came out against a blank.
```

Numbers found in both documents become hard constraints, not hints. Content you
marked `5` is compared only against content you marked `5`, whatever the
similarity score says, so **a section you numbered in both files cannot come out
against a blank.** Three things make that hold:

- a reconstructed paragraph running through two of your marks is split at the
  boundary, so a mark cannot be swallowed by its neighbour;
- prose inside a section is compared as one passage per side, because the two
  documents rarely break a section into the same number of paragraphs — pairing
  those counts against each other is what leaves passages facing a blank — and
  the word-level diff still locates the differences inside it;
- a line one document read as a table row and the other read as prose is handed
  to the prose comparison rather than shown against a blank.

Table rows inside a numbered section are still compared individually, so a
figure is still checked against its counterpart cell by cell.

The report badges each section `§n`, and offers a filter to hide everything
outside your marks. Marking is entirely optional, partial marking is fine, and
anything unmarked falls back to content matching. `--ignore-marks` turns it off.

**A mark that covers less than you intended will show as a difference** — that
is the feature working, not failing. If a section reports text present on one
side only, check the highlight actually covers the whole passage on both.

### Finding a point back on the page

The report says what differs. `--marked-pdfs` says where it came from:

```bash
fincheck compare exhibit.pdf original.pdf \
    --side-by-side review.html --marked-pdfs
#   Numbered copy written to: exhibit.marked.pdf
#   Numbered copy written to: original.marked.pdf
```

Every section in the report carries a **serial number**, and the same number is
stamped onto the passage it was drawn from in a copy of *each* source PDF. Read
a point in the report, note its number, and it is outlined and labelled on the
page in both copies.

The copies stand on their own too — the outlines are coloured by verdict, so a
page can be skimmed without the report: **green** where the two documents
agree, **amber** where they differ, **red** where the section exists on one side
only. Where you numbered a section yourself, the badge shows `§n` under the
serial, so your numbering and the report's line up.

Page numbers throughout the report are **links into those copies at that page** —
the section headers, and every row's page in the gutter. Paths are written
relative, so the report and its two copies can be moved or shared as a set.

### What it infers, and why that matters

Each of these rules can misfire, and none of them is evidence:

- spans sharing a text baseline form a row (exact — baselines come from the file);
- a row is a figure row when its numbers sit to the right of a short label, so
  prose quoting a figure mid-sentence stays prose;
- consecutive figure rows form a table, consecutive prose rows a paragraph, with
  a break where the vertical gap exceeds the page's usual line pitch;
- a heading between figure rows stays inside its table;
- a block continues across a page break when the text before it does not end a
  sentence, or when a table resumes with the same column count;
- pairing is a similarity judgement above a floor.

Read a difference here as "look at this", and use the exact layers when you need
proof. In library form:

```python
from fincheck import side_by_side

result = side_by_side("draft.pdf", "final.pdf", output_html="review.html")
for pair, cells in result.figure_changes():
    for column, before, after in cells:
        print(pair.a.text, f"col {column}:", before, "->", after)
```

### Does it work on any pair of PDFs?

**The exact comparison: yes, unconditionally.** Bytes, geometry, spans, graphics
and pixels involve no tuning and no document model, so they behave the same on a
statement, a contract or a scan.

**The side-by-side: yes for text-based, single-column documents**, financial or
not. Tested on deliberately unlike pairs:

| Document | Result |
|---|---|
| Financial statements, two different toolchains | works; figure-level differences |
| Legal contract — prose, numbered clauses, no tables | works; amendments as word diffs |
| Statement using `1.234.567,89` (decimal comma) | works; figure-level differences |
| Table with **left**-aligned figures | works; figure-level differences |
| **Two-column page layout** (academic paper) | ⚠ columns merge — see below |
| **Scanned pages**, no text layer | ⚠ nothing to align — use the exact layers |

Two limitations are real and worth knowing before you trust a run:

- **Multi-column page layouts merge.** Rows are built from shared text
  baselines, so on a two-column page the left and right columns land on the same
  baseline and become one row. Differences still surface, but they are attributed
  to a merged passage rather than to the column they came from. Financial
  statements are single-column, which is why this has not been addressed;
  comparing academic or newspaper layouts would need column detection first.
- **Scanned pages have nothing to reconstruct.** With no text layer there are no
  paragraphs or tables to pair, so the side-by-side is empty and correctly says
  so. The exact comparison still works — the image hash and pixel layers catch
  the difference. OCR the files first if you need content-level comparison.

Beyond those, the rules are heuristics and any of them can misfire on a layout
they were not written for. The exact layers are always there to fall back on.

### What no layer can do

Attributing a changed figure to a named line item *and column heading* needs
structure the PDF does not carry. The side-by-side gets close — it will tell you
that the third figure on the *Balance as at April 1* row differs — but naming
that column means reading a wrapped, multi-level table header, which is
reconstruction again. If you need that, compare the XBRL instance and
calculation linkbase, or the source workbook.
