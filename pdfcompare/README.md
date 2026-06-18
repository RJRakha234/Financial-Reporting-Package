# pdfcompare — compare two PDFs' text with **only the Python standard library**

Built for **air-gapped / locked-down corporate environments** where you cannot
`pip install` anything. It imports nothing outside the standard library
(`zlib`, `base64`, `difflib`, `hashlib`, `argparse`, `re`). Copy the single file
`pdfcompare.py` onto the machine and run it — no setup, no network, no installs.

## Why this is non-trivial

Python has **no built-in PDF parser**. `pdfcompare` does the parsing itself:

1. Scans the PDF for indirect objects (without trusting the often-broken
   cross-reference table).
2. Decompresses content streams — **FlateDecode/zlib**, ASCIIHex, ASCII85, LZW —
   and expands PDF 1.5 object streams.
3. Walks the page tree and pulls the text-showing operators (`Tj`, `TJ`, `'`,
   `"`) out of each page, using each font's **ToUnicode CMap** when present so
   modern subset / Identity-H fonts decode correctly.
4. Diffs the resulting text with `difflib`.

## Usage

```bash
# Diff two PDFs (red = removed, green = added in a color terminal)
python pdfcompare.py old.pdf new.pdf

# Ignore whitespace-only changes, show more context
python pdfcompare.py old.pdf new.pdf --ignore-whitespace --context 5

# Compare page-by-page
python pdfcompare.py old.pdf new.pdf --by-page

# Machine-readable report
python pdfcompare.py old.pdf new.pdf --json

# Just dump the text the tool extracted (debugging / sanity check)
python pdfcompare.py file.pdf --dump

# Force or disable color
python pdfcompare.py old.pdf new.pdf --color always
```

Exit status: `0` if the extracted text is identical, `1` if it differs, `2` on
error. Handy in CI or a pipeline.

## Does it "highlight" the differences?

- **In the terminal: yes.** Removed lines are shown in red, added lines in
  green, hunk headers in cyan (standard unified-diff `-`/`+` markers, colorized).
- **A highlighted *output PDF*** (colored boxes drawn back onto the original,
  like the sibling `fincheck` tool does) is **not** produced — that needs a PDF
  *writer*, which the standard library does not have. The stdlib can read and
  decompress PDFs but cannot author annotated ones reliably.

## Honest limitations

- **Scanned / image-only PDFs** contain no text — they need OCR, which the
  standard library cannot do. The tool detects this and tells you.
- **Encrypted PDFs** are not decrypted (the tool warns you).
- Some embedded subset fonts ship **no ToUnicode map**; their glyphs can't be
  turned back into characters without the font program. The tool reports which
  pages had this problem so you know when to treat a diff as "review manually".

For full-fidelity extraction on difficult PDFs you would want `pdfplumber` /
`PyMuPDF` (see the `fincheck` tool) — but those require installing native
packages. If your environment has an **internal PyPI mirror** (Artifactory /
Nexus), `pip install pdfplumber` may work without public internet; ask your
platform team for the index URL.

## Tests

```bash
# Standalone (no pytest needed — ideal for locked-down boxes)
python test_pdfcompare.py

# Or with pytest if you have it
python -m pytest
```

`make_test_pdfs.py` hand-builds tiny valid PDFs (uncompressed and
FlateDecode-compressed) using only the standard library, so the whole suite runs
offline.
