# xbrlextract — filed XBRL → reviewable Excel

`xbrlextract` turns a **filed XBRL instance** (the `*_htm.xml` that EDGAR extracts
from an inline-XBRL filing) into a **multi-sheet Excel workbook** you can read,
sort, filter and validate. The XBRL stores values, periods and units in separate
places linked by id; this tool re-joins them into one row per tagged fact, and
uses the extension schema's embedded linkbases to add human-readable labels, the
statement structure and the calculation (footing) relationships.

It is the extraction step that makes **tagging review / validation** possible —
once the facts are in a spreadsheet, you can check element selection, signs,
scale, units, periods and dimensions by eye or with a downstream checker.

## Install

```bash
pip install -r requirements.txt
```

## Use it (command line)

```bash
# Point it at the extracted instance; the .xsd next to it is auto-detected
python -m xbrlextract infy-20260331_htm.xml

# Or be explicit about the schema and the output path
python -m xbrlextract infy-20260331_htm.xml -s infy-20260331.xsd -x infy_facts.xlsx
```

```
Extracted 4050 facts (711 concepts, 1358 contexts)
  labels & structure from: infy-20260331.xsd
Workbook written to: infy-20260331_htm.facts.xlsx
```

The schema is optional — without it you still get facts/contexts/units, just with
concept names instead of friendly labels.

## Use it (library)

```python
from xbrlextract import extract

result = extract("infy-20260331_htm.xml",
                 schema="infy-20260331.xsd",
                 output_xlsx="infy_facts.xlsx")
print(result.fact_count, "facts,", result.concept_count, "concepts")
```

## What you get (the workbook)

| Sheet | What it holds |
|---|---|
| **Overview** | Registrant, document type, period end, CIK, and the counts. |
| **Facts** | **One row per tagged value** — the core extract: `Fact ID · Concept (QName) · Label · Type · Value · Unit · Decimals · Period · Start · End · Dimensions · Negated label · Context`. Auto-filtered. |
| **Contexts** | Every period / dimension context, resolved (entity, period, axis = member). |
| **Units** | Unit definitions — currencies, shares, per-share divide units, custom enumerations. |
| **Concepts** | Every concept used: data type, period type, debit/credit balance, abstract, base-vs-extension, negated, and fact count. |
| **Statements** | Index of presentation roles (statements & disclosures) with concept/calc counts. |
| **Presentation** | The ordered, indented concept outline for each statement. |
| **Calculation** | `parent = Σ(child × weight)` footing relationships from the calculation linkbase. |

## Why those columns — they map to tagging-error classes

| Column(s) | What a reviewer checks |
|---|---|
| Concept (QName) | Right element chosen? Same element as prior year? Extension where a standard tag exists? |
| Type / Unit | Monetary in the right currency (INR vs USD)? Per-share using a divide unit? Shares vs pure? |
| Value / Decimals | Value off by a factor (scale)? Precision sensible? |
| Negated label | Concept displayed with a flipped sign — confirm the stored sign is intended. |
| Period / Start / End | A full-year (duration) figure stuck on an instant context, or vice versa? |
| Dimensions | Attached to the right axis & member (segment, geography, class)? |
| Calculation sheet | Does each total foot against the sum of its tagged children? |

## How it works

1. **Instance** (`instance.py`) — parse the `*_htm.xml`: build id→context and
   id→unit lookups, then walk every element carrying a `contextRef` and resolve
   it into a flat `Fact` (concept, value, unit, decimals, period, dimensions).
2. **Taxonomy** (`taxonomy.py`) — parse the `.xsd` and its **embedded linkbases**:
   extension element declarations (type / period / balance), the label linkbase
   (resolved loc → labelArc → label, including `negatedLabel`), the role
   definitions (statement names), and the presentation & calculation networks.
3. **Workbook** (`workbook.py`) — join facts to labels/structure and write the
   sheets above with `openpyxl`.

## Scope & assumptions

- Reads the **EDGAR-extracted instance** (`*_htm.xml`) plus the extension `.xsd`
  with embedded linkbases (the two XBRL "Data Files" on the filing index page).
- Standard-taxonomy (e.g. `ifrs-full`) concept *declarations* live in remote
  schemas and are not fetched; their **labels** are still resolved when the filer
  embedded them (Infosys does), otherwise the concept name is humanised.
- **Offline** — it reads local files and writes a local workbook; no network calls.
- Extraction only — it does not (yet) run the validation checks; it produces the
  reviewable table those checks run on.
