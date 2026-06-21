# plcheck — automate the IFRS INR P&L "check" file

`plcheck` rebuilds, in one step, the reconciliation **check workbook** for the
IFRS INR P&L report. You hand it the report and the three inputs it was built
from; it reconciles them, tells you what doesn't tie, and writes a
self-contained, **formula-driven** check workbook with every difference
highlighted — exactly the file you would otherwise assemble by hand.

It was reverse-engineered from a manually prepared check file and reproduces
its results to the cent (including the genuine rounding discrepancies that file
was catching).

## What it checks

For every entity column (CHF / EUR / DKK …), three reconciliations are run, each
expressed as a **difference that should be zero**:

1. **LC tie-out** — every local-currency figure in the report must equal its
   source:
   * income, tax, interest and similar accounts tie back to the **Real Time
     Trial Balance**;
   * the functionally-split expense accounts (Cost of Production, Sales,
     General Administration) tie back to the matching block of the **Aggregate
     Expenses** report.
2. **GC (INR) conversion** — each group-currency *Balance* equals
   `(LC Balance + LC Consol) × FX rate`, using the **MA Rates** table.
3. **GC consolidation** — each group-currency *Total* equals
   `GC Balance + Reclass + Elimination + Consol`.

Plus a **net-profit reconciliation**: the report's net profit must tie to the
net profit implied by the Real Time TB, and `Income + Expense + Net Profit` must
net to zero internally.

## Install

```bash
pip install -r requirements.txt    # just openpyxl
```

## Use it (command line)

```bash
python -m plcheck PL_Report.xlsx \
    --tb    Real_Time_TB.xlsx \
    --agg   Aggregate_Expenses.xlsx \
    --rates MA_Rates.xlsx \
    -o      PL_Report_Check.xlsx
```

This writes `PL_Report_Check.xlsx` (a `Check` sheet plus the three sources
embedded as sheets so the formulas resolve) and prints a summary:

```
✗ 10 difference(s) exceed ±0.5:

  · LC tie-out       General Administration / 290100 Depreciation
      BALSDE: stated 458.00  expected 458.51  (off by 0.51)
  · LC tie-out       General Administration / 290100 Depreciation
      BALSDK: stated 157,384.00  expected 157,384.65  (off by 0.65)
  · FX conversion    General Administration / 290100 Depreciation
      BALSDE: stated 49,436.55  expected 49,381.56  (off by -54.99)
  ...
✗ Net-profit reconciliation off for: BALSDE, BALSDK
```

Other options:

```bash
python -m plcheck PL_Report.xlsx --tb ... --agg ... --rates ... --json
python -m plcheck PL_Report.xlsx --tb ... --agg ... --rates ... -o none   # report only
python -m plcheck ... --tolerance 1.0
```

The process exits `1` when something doesn't reconcile and `0` when everything
ties — handy in a pipeline. Try it on the bundled sample:

```bash
python -m plcheck sample/IFRS_INR_PL_Report.xlsx \
    --tb sample/Real_Time_TB.xlsx --agg sample/Aggregate_Expenses.xlsx \
    --rates sample/MA_Rates.xlsx -o /tmp/Check.xlsx
```

## Use it (library)

```python
from plcheck import analyze

result = analyze("PL_Report.xlsx", "TB.xlsx", "AggExp.xlsx", "Rates.xlsx",
                 output_path="PL_Report_Check.xlsx")
print(result.ok)                       # False when something doesn't tie
for d in result.evaluation.flagged():
    print(d.kind, d.category, d.account, d.entity, "off by", round(d.delta, 2))
```

## The generated workbook

The output is a real, recalculating Excel file — not a static dump:

* the **`Check` sheet** mirrors the report's column blocks and inserts a *Diff*
  column after every entity column of the three reconciled blocks (LC-Balance,
  GC-Balance, GC-Total). Each Diff cell is a live formula
  (`VLOOKUP`/`SUMIF`/`HLOOKUP`) referencing the embedded source sheets;
* a **Sum of Differences** row totals each Diff column (should be ~0);
* a **net-profit reconciliation block** ties the report to the TB;
* **conditional formatting** turns any cell whose absolute difference exceeds
  the tolerance red, so issues are visible the moment you open it in Excel.

Because everything is a formula over the embedded inputs, an auditor can trace
every number, and tweaking an input recalculates the check.

## Robust to revised files

The header band is read by **label**, not by fixed cell addresses, and the
source ranges are derived from each input's actual size. So a revised report
with different numbers — or extra accounts, entities, or rows — is handled
without code changes. The mapping that encodes *which* category is income vs.
expense and *where* each ties back lives in `plcheck/config.py`
(`DEFAULT_CATEGORY_RULES`); unknown categories are reported, never guessed.

Two things this guarantees in particular:

* **No GL is ever missed.** The report is read row by row, so however many GL
  accounts a section contains — more or fewer than before — each one receives
  the full set of checks for its section (LC tie-out, FX conversion,
  consolidation). The Sum-of-Differences row and the net-profit block stretch
  to cover them automatically.
* **The TB net profit is computed, not borrowed.** "Net Profit as per Real Time
  TB" is summed live (`SUMIFS`) over every P&L-series GL in the trial balance
  (accounts `100000`–`399999`, i.e. the 1-, 2- and 3-series), **not** read from
  the TB's own `SUM(...)` row. So if GLs are added to or removed from the TB the
  net profit still ties, and balance-sheet accounts (4-, 8-, 9-series) are
  correctly excluded. The range is configurable via `CheckConfig`
  (`pl_account_low` / `pl_account_high`).

## Notes & assumptions

* The MA-rate lookup uses the **Exchange Rate** column directly (matching the
  source check file). Every current entity currency has a `From Ratio` of 1; if
  you add a currency quoted per 100 units (e.g. HUF, JPY), divide the rate by
  its ratio in the rate table first.
* The local-currency *Overall Result* columns are carried through but not
  re-checked (they are the report's own row sums).
* "P&L accounts" for the TB net-profit sum are identified by their **leading
  digit** (the 1-, 2- and 3-series; 4/8/9-series balance-sheet accounts are
  excluded). This works whether the TB stores account numbers as **numbers or
  as text** (common in ERP exports) and for any account length. Adjust the
  series via `pl_series` in `CheckConfig`.
* Block headings (`LC - Balance`, `GC - Total`, …) are matched **ignoring
  spacing and case**, so minor export differences (`GC-Total`, `gc  -  total`)
  still work. If an expected block is genuinely absent (or named completely
  differently) its check is skipped and a clear ⚠ warning is printed.
* The FX-rate lookup spans **150 rows** of the MA Rates table by default
  (`rate_lookup_rows`), so a varying number of currencies is always covered.
* `--tolerance` (default `0.5`) is the absolute slack, in each figure's own
  units, before a difference is flagged.

## How it works

1. **`inputs.py`** parses the report into a typed table and works out the
   geometry of the three source sheets.
2. **`evaluate.py`** recomputes the three reconciliations in Python — this
   drives the console/JSON summary and the exit code, with no Excel engine
   required.
3. **`layout.py`** plans the `Check` sheet's columns (value + Diff).
4. **`workbook.py`** writes the live formulas, embeds the sources, and applies
   the highlighting.

Run the tests with `python -m pytest`.
