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
python3 -m pytest tests/ -q      # 126 tests, ~3 seconds
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
| `comparison_report.html` | Self-contained review console. Open it in any browser. |
| `source_annotated.pdf` | The benchmark with everything compared tinted by outcome, sections numbered, bookmarks per section. |
| `compared_annotated.pdf` | The same for the filed document. |
| `source_combined.pdf` | Only when several source files were joined — the exact benchmark that was compared. |

Keep the three files **in the same folder**; the report links into the PDFs.

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

## 4. Using the report

- **Accept / Reject** on each deviating section; progress is saved in your
  browser and exports to **CSV or JSON**.
- **Side by side · Tracked changes · Before · After** — set globally in the
  toolbar, or per section from its own header.
- **Click any cell** to open the highlighted source page inside the report,
  with the exact passage spotlighted.
- **Only undecided** hides what you have already settled.

## 5. Marking sections by hand (optional)

Unmarked documents are sectioned automatically — each page of the benchmark
becomes a section — which on a real 41-page filing scored 95% with nothing left
against a blank. Hand-marking buys a little more (97%) and is worth it when you
want one-sided content listed as such:

In Acrobat (or any PDF reader that writes highlight comments), highlight a
region and type a **number** as the comment. Number the corresponding regions
identically in both PDFs. Content marked `7` is then compared only against
content marked `7`.

## 6. Other commands

```bash
# Exact comparison — bytes, glyphs, vectors, rendered pixels. Proof, not inference.
python3 -m fincheck compare old.pdf new.pdf
python3 -m fincheck compare old.pdf new.pdf --json

# Check that every total and subtotal in one statement actually foots
python3 -m fincheck statements.pdf            # writes statements.highlighted.pdf
```

## 7. Using it as a library

```python
from fincheck import side_by_side

result = side_by_side(
    "source.pdf", "filed.pdf",
    output_html="report.html",
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
