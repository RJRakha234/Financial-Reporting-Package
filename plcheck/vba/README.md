# plcheck — Excel VBA edition (no Python, no libraries)

`PLCheck.bas` reproduces the whole plcheck tool **inside Excel itself**. It uses
nothing but Excel + VBA — no Python, no `pip`, no add‑ins, no external
libraries — so it can't be broken by a Python or library upgrade, and there is
nothing to install. It runs anywhere Excel runs.

It builds the same two outputs as the Python tool:

* a **Check** sheet — LC tie‑out, GC FX conversion, GC consolidation and the
  net‑profit reconciliation, all as live formulas with non‑zero differences
  highlighted red;
* a **Minority Interest** sheet — Net Profit (GC‑Bal), Dividend (GL 332010),
  Profit before Dividend, Minority total (GC‑Total) and Current Period %.

It handles **IFRS INR**, **Ind‑AS Function‑wise** and **Ind‑AS Nature‑wise**
reports from the one module (Nature‑wise = no Aggregate Exp; just leave the Agg
path blank), a varying number of GLs / companies, text‑formatted account
numbers, header bands that start on row 1 or row 2, and companies missing from
the TB.

## One‑time setup

1. Open Excel → press **Alt + F11** (the Visual Basic editor).
2. **File ▸ Import File…** and choose `PLCheck.bas`
   (or **Insert ▸ Module** and paste the whole `PLCheck.bas` text in).
3. Close the editor. In the workbook, add a sheet named **`Control`** and put
   the input file paths in column B:

   | | A | B |
   |--|--|--|
   | 1 | Report | `C:\…\IFRS / INDAS PL Report.xlsx` |
   | 2 | TB | `C:\…\Real_Time_TB.xlsx` |
   | 3 | Agg | `C:\…\Aggregate_Expenses.xlsx`  *(leave blank for Nature‑wise)* |
   | 4 | Rates | `C:\…\MA_Rates.xlsx` |
   | 5 | GC currency | *(optional — auto‑detected; only set to force `INR`/`USD`)* |

   **Function‑wise** reports (IFRS INR, Ind‑AS Function‑wise) need the Aggregate
   Expenses path in **B3**. For an **Ind‑AS Nature‑wise** report there is no
   Aggregate Expenses file — **leave B3 blank** and every line ties straight to
   the Real Time TB. The **group/consolidation currency** (INR vs USD) is
   **auto‑detected** from the report, so you can leave **B5 blank**; set it
   (`INR`/`USD`) only if you want to force it. GC figures use the cross‑rate
   `local→INR ÷ GC→INR`.

4. Save the workbook as **macro‑enabled** (`.xlsm`).

## Run it

* **Developer ▸ Macros ▸ `GenerateCheckFile` ▸ Run**, or
* draw a button (Developer ▸ Insert ▸ Button) and assign `GenerateCheckFile`.

It imports the four inputs as sheets (`Real Time TB`, `Aggregate Exp`,
`MA rates`, `PL Report`), builds `Check` and `Minority Interest`, then tells you
when it's done. Use **File ▸ Save As** to keep the result. Re‑running refreshes
the sheets.

## Adjusting it

Everything report‑specific is in two places near the bottom of the module:

* `CatClass` / `CatSource` — map each column‑A **section name** to its
  classification (Income / Expense / Net Profit) and its source (the Real Time
  TB, or a block of the Aggregate Expenses report). Add a new wording here if a
  report uses a section name that isn't listed — the macro warns you about any
  unrecognised section when it runs.
* The constants at the top — tolerance, the dividend GL (`332010`), the P&L
  account leading digits (`123`), and the FX‑rate lookup window.

## Notes

* If macros are blocked by policy, ask IT to allow this workbook (it can be
  code‑signed). Macro execution is a standard Excel feature.
* The Python edition (in the parent folder) remains available and is the
  reference implementation; this VBA edition mirrors its logic and output.
