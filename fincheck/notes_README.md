# fincheck.notes — common-notes comparison across financial statements

At quarter-end the same entity/period is reported in **four** statements:

| Document            | Framework | Entity        | Currency |
|---------------------|-----------|---------------|----------|
| Consolidated Ind AS | Ind AS    | Consolidated  | INR      |
| Standalone Ind AS   | Ind AS    | Standalone    | INR      |
| IFRS (INR)          | IFRS      | Consolidated  | INR      |
| IFRS (USD)          | IFRS      | Consolidated  | USD      |

The notes shared across these documents should say the **same thing**, *subject
to* a small set of expected differences:

* **entity** — "the Company" (standalone) vs "the Group" (consolidated);
* **framework** — "Ind AS 116" vs "IFRS 16" / "IAS 1";
* **currency** — "₹ … crore" vs "US$ … million", and the monetary amounts.

`fincheck.notes` automates that review. It extracts each note's narrative
(accounting-policy / disclosure prose), neutralizes the expected differences,
and surfaces only the **substantive wording differences** in an interactive,
offline HTML report where a checker can **Accept** or **Ignore** each one.
Decisions are saved to a JSON ledger, so once an expected difference is signed
off it stays out of the way next quarter — unless the wording actually changes.

> Scope: this compares **narrative text** only. Table figures are *not*
> compared (they legitimately differ standalone↔consol and INR↔USD). The
> alignment matrix still flags a note that is missing from a document.

## Use it

```bash
pip install -r requirements.txt   # pdfplumber (PyMuPDF only needed by the footing checker)

python -m fincheck.notes \
    consol_indas.pdf:"Consol Ind AS" \
    standalone_indas.pdf:"Standalone Ind AS" \
    ifrs_inr.pdf:"IFRS INR" \
    ifrs_usd.pdf:"IFRS USD" \
    -o notes_diff.html
```

Then open `notes_diff.html` in any browser. Mark each difference **Accept** /
**Ignore**, click **Export decisions** to save `decisions.json`, and next
quarter pass it back to suppress the ones you already resolved:

```bash
python -m fincheck.notes <four PDFs> -o notes_diff.html --ledger decisions.json
```

The process exits non-zero while any difference is still **open**, so it can
gate a quarter-end checklist.

### Document specs

Each `DOC` argument is `path[:label][@kind]`:

* `label` — display name in the report (defaults to the file name);
* `kind` — `framework-entity-currency`, e.g. `ifrs-consol-usd`
  (`framework`: `indas`|`ifrs`, `entity`: `standalone`|`consol`,
  `currency`: `inr`|`usd`). Omit it to let the tool infer the kind from the
  cover pages; specify it when a document's wording defeats the heuristic.

```bash
python -m fincheck.notes statements.pdf:"IFRS USD"@ifrs-consol-usd ...
```

## Library API

```python
from fincheck.notes import load_document, compare_documents, write_html_report, load_ledger

docs = [
    load_document("consol_indas.pdf", name="Consol Ind AS"),
    load_document("ifrs_inr.pdf", name="IFRS INR"),
]
result = compare_documents(docs)
print(result.common_topics)                 # notes shared by all documents
for pair in result.pairs:
    print(pair.left_doc, pair.right_doc, pair.similarity, len(pair.differences))

write_html_report(result, "notes_diff.html", ledger=load_ledger("decisions.json"))
```

## How it works

1. **Sections** (`sections.py`) — reuses `fincheck.extract` to rebuild rows, then
   detects note headings (`2.7 Trade receivables`, including the `X`-glyph and
   split-number variants seen in these PDFs) and isolates each note's prose,
   filtering out interleaved table scaffolding.
2. **Normalize** (`normalize.py`) — matches notes across documents by a
   **canonical topic** (so "Goodwill and *other* intangible assets" ≡ "Goodwill
   and intangible assets"), and builds a **fingerprint** of each note's prose
   that strips whitespace (intra-word spacing is unreliable in these PDFs) and
   collapses entity/framework/currency/amount variation to neutral tokens.
3. **Compare** (`compare.py`) — diffs the two fingerprints character-wise
   (robust to line-wrapping and segmentation), projects each differing region
   back onto the original readable prose, coalesces neighbours, and drops
   trivially short noise. Each difference gets a stable content hash.
4. **Report / ledger** (`report.py`, `ledger.py`) — renders the offline HTML
   review UI and persists accept/ignore decisions keyed by that hash.

### Tuning

* Canonical-topic merges live in `_TITLE_ALIASES` (`normalize.py`).
* What counts as an *expected* (neutralized) difference is the pattern set in
  `normalize.py` (`_STD_REF_RE`, `_SCOPE_RE`, `_ENTITY_RE`, `_CURRENCY_RE`,
  `_UNIT_RE`, `_NUMBER_RE`) — extend these to neutralize more, or remove one to
  start flagging that category.
* Diff sensitivity / coalescing: `_MIN_DIFF_CHARS`, `_COALESCE_GAP`, `_CONTEXT`
  in `compare.py`.

## Offline & privacy

Like the rest of `fincheck`, this is **fully offline** — it reads PDFs from disk
and writes the HTML report back to disk, with no network calls. The report is a
single self-contained file (CSS/JS inlined); accept/ignore state lives in the
browser's `localStorage` and in the `decisions.json` you export.

## Known limitations

* Comparison is on narrative prose; table line-items and figures are out of
  scope by design.
* A short prose sentence sitting directly between two table rows may be dropped
  with the surrounding scaffolding. Because the same heuristic applies to every
  document, this rarely affects the *differences* surfaced.
* American/British spelling (e.g. "recognize"/"recognise") is intentionally
  **not** neutralized, so it shows up as a (small) difference to accept.
