# Quickstart

## 1. Install

```bash
python3 -m pip install pymupdf pdfplumber
```

Python 3.10+. PyMuPDF does the reading and annotating; pdfplumber is used by
the totals checker. Nothing else is required, and the tool makes **no network
calls of any kind** — it is safe on an air-gapped machine.

Optional: `pip install pytest` to run the test suite, `reportlab` for
`make_sample.py`.

## 2. Verify the install

```bash
cd fincheck
python3 -m pytest tests/ -q      # 140 tests, ~4 seconds
```

## 3. Compare a filing against its source

The main use: a **source document** (the benchmark) versus the HTML-converted
PDF filed from it.

```bash
python3 -m fincheck compare \
    --source  source_statements.pdf \
    --compared filed_exhibit.pdf \
    --output  ./results/
```

A reporting package split across files is one benchmark — give them in order:

```bash
python3 -m fincheck compare \
    --source  auditors_report.pdf statements.pdf \
    --compared filed_exhibit.pdf \
    --output  ./results/
```

### What you get in `./results/`

| File | What it is |
|---|---|
| `comparison_report.html` | Side-by-side reading report: every passage beside its counterpart, tracked changes, match scores, in-page previews of the highlighted source. |
| `review_console.html` | Working surface: one card per item, Accept/Reject on every difference, progress bar, CSV/JSON export. |
| `source_annotated.pdf` | The benchmark with everything compared tinted by outcome, sections numbered, bookmarks per section. |
| `compared_annotated.pdf` | The same for the filed document. |
| `source_combined.pdf` | Only when several source files were joined — the exact benchmark that was compared. |

Keep these **in the same folder**; both HTML files link into the PDFs.

### Reading the console output

```
Composite match with benchmark: 95%
  735 segments matched (41 passages, 694 table rows); 123 deviate, 314 figure cells deviate.
  0 in the benchmark only, 0 not in the benchmark, 0 section(s) moved.

Figure ledger: 2,238 figures in the benchmark, 2,084 in the compared document.
  155 printed figure(s) in the benchmark only: 20 distinct amount(s) ...
          88,116  p7  (not printed in the other document)  2,071 54 169 616 ...
```

The **figure ledger** is the part to read first and trust most: it reconciles
every printed figure as plain multisets with no pairing anywhere in the chain,
so it cannot be misled by a mis-matched row. `not printed in the other
document` means exactly that.

Exit status is `0` when nothing deviates and `1` otherwise, so it drops
straight into a release gate.

## 4. Comparing against the HTML instead of a PDF of it

If the filed exhibit is HTML, compare **that**, not a PDF printed from it:

```bash
python3 -m fincheck compare \
    --source  statements.pdf \
    --compared exv99w08.htm \
    --output  ./results/
```

This matters more than it sounds. Printing an exhibit to PDF clips wide
tables at the page margin and can mangle currency signs. Measured on a real
filing, **every** amount the PDF comparison reported as missing was present in
the HTML — a 100% false-alarm rate — while the same run against the HTML
reported none. The tool detects real changes either way; it is specificity
that the conversion destroys.

Trade-off: an HTML filing has no pages, so no annotated copy is written for
that side, and the row-level pairing is looser (an Excel-exported PDF and
EDGAR markup group rows differently). Run both — the PDF pair for the readable
worksheet and the annotated copies, the HTML pair for the reconciliation you
rely on.

## 5. Using the report

- **Accept / Reject** on each deviating section; progress is saved in your
  browser and exports to **CSV or JSON**.
- **Side by side · Tracked changes · Before · After** — set globally in the
  toolbar, or per section from its own header.
- **Click any cell** to open the highlighted source page inside the report,
  with the exact passage spotlighted.
- **Only undecided** hides what you have already settled.

### The review console

`review_console.html` is the operating surface rather than the reading one:

- One card per item, in order, one column per document.
- Differences marked **word by word**. Orange = words the benchmark has that
  this document does not; blue = words here the benchmark lacks.
- **Track changes / Before / After** moves only the strikethrough — the text
  and both colours stay on screen, so nothing re-flows under you. Global, or
  per cell.
- **Accept / Reject** on every difference, with a live progress bar. Benchmark
  cells, matching cells and absent content need no decision and are excluded
  from the count.
- Decisions save to the browser and survive a reload; export as CSV or JSON.

## 6. Marking sections by hand (optional)

Unmarked documents are sectioned automatically — each page of the benchmark
becomes a section — which on a real 41-page filing scored 95% with nothing left
against a blank. Hand-marking buys a little more (97%) and is worth it when you
want one-sided content listed as such:

In Acrobat (or any PDF reader that writes highlight comments), highlight a
region and type a **number** as the comment. Number the corresponding regions
identically in both PDFs. Content marked `7` is then compared only against
content marked `7`.

## 7. Other commands

```bash
# Exact comparison — bytes, glyphs, vectors, rendered pixels. Proof, not inference.
python3 -m fincheck compare old.pdf new.pdf
python3 -m fincheck compare old.pdf new.pdf --json

# Check that every total and subtotal in one statement actually foots
python3 -m fincheck statements.pdf            # writes statements.highlighted.pdf
```

## 8. Using it as a library

```python
from fincheck import side_by_side

result = side_by_side(
    "source.pdf", "filed.pdf",
    output_html="report.html",
    console_html="review_console.html",
    marked_pdf_a="source_annotated.pdf",
    marked_pdf_b="filed_annotated.pdf",
)

print(result.summary.overall)             # composite match, 0-100
print(result.ledger.coverage)             # % of benchmark figures found
for x in result.ledger.absent_amounts:    # printed in the benchmark, nowhere else
    print(x.value, "page", x.occurrences[0].page, x.occurrences[0].context)

for pair, cells in result.figure_changes():
    print(pair.a.text, cells)             # [(column, benchmark, compared), ...]
```

## Three tiers of trust

They are **not** equally reliable, and the report says so on its own face:

1. **Exact comparison** (`fincheck compare`) — bytes, glyphs, vectors, pixels.
   Proof. Infers nothing.
2. **Figure ledger** — exact on whether any amount was lost or invented. This
   is the layer to sign against.
3. **Side-by-side view** — a reviewer's worksheet. Structure is reconstructed
   from page geometry and pairing is a similarity judgement. Read a flag as
   "look at this", never as "this is wrong".

See `README.md` for the full failure-mode catalogue.
