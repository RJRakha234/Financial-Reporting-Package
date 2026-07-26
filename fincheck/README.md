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

The page opens with an index of the sections that differ on both sides, has
filters for hiding identical and one-sided sections, and folds away the wrapped
label lines a narrower page produces (counted, not dropped).

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

### What no layer can do

Attributing a changed figure to a named line item *and column heading* needs
structure the PDF does not carry. The side-by-side gets close — it will tell you
that the third figure on the *Balance as at April 1* row differs — but naming
that column means reading a wrapped, multi-level table header, which is
reconstruction again. If you need that, compare the XBRL instance and
calculation linkbase, or the source workbook.
