# finround for Excel — VBA

The same controlled rounding as [`finround`](../finround), as two importable
VBA modules. Select a table in Excel, run one macro, and get back a table where
every figure is rounded **and every total still equals the sum of its rounded
parts** — down the rows and across the columns at once, through nested
subtotals.

Round each cell on its own and this happens:

```
        Q1   Q2   Q3  Total          10 + 20 + 30 = 60, not 61   ✗
North   10   20   30     61          40 + 50 + 60 = 150, not 151 ✗
South   40   50   60    151          70 + 80 + 90 = 240, not 241 ✗
East    70   80   90    241
Total  121  151  181    454          all eight totals wrong
```

Run `FinRoundSelection` on the same figures and every one of those eight
checks foots, with no figure moved as much as a whole step.

## Install

1. Open the workbook, press **Alt + F11** (Tools ▸ Macros ▸ Edit Basic on Mac).
2. **File ▸ Import File…** and import **both**:
   - `FinRound.bas` — the arithmetic. No Excel objects; nothing to configure.
   - `FinRoundExcel.bas` — the worksheet front end.
3. Back in Excel, save as `.xlsm` (a plain `.xlsx` will not keep the macros).

Import both or neither: the front end calls the core.

**Run `FRSelfTest` first.** In the Immediate window (Ctrl+G) type
`?FRSelfTest` and press Enter. It rounds five known tables — including nested
subtotals, negatives and a 13×8 block — and prints `ALL PASSED` if the module
is working. It touches no worksheet.

## Use it

**Select the table including its heading row and label column**, then run
`FinRoundSelection` (Alt+F8). It asks two questions:

| Prompt | Answer |
| --- | --- |
| Divide every figure by | `1` to leave as is, `1000` thousands, `100000` lakh, `10000000` crore, `1000000` millions |
| Decimal places | `0`, `1`, `2` … |

A new sheet appears with the rounded table, the total rows and columns in bold,
**the figures that had to move off their nearest value shaded amber**, and a
report: how many total checks foot, how many figures moved, and how much the
consistency cost against a straight rounding.

Totals are found the same way as in the Python version: a row labelled
*Total*, *Subtotal*, *Total assets* … is matched against the longest block of
rows above it that adds up to it **in every column at once**. Nested subtotals
work to any depth. A row that reads like a total but that nothing adds up to is
reported and left out — never invented.

### From your own code

```vba
' round A1:F12 (labels included) into H1
Dim problem As String
problem = FinRoundToRange(Range("A1:F12"), Range("H1"), 1000, 0.1)
If Len(problem) > 0 Then MsgBox problem
```

```excel
=FR_ROUND(A1:F12, 1000, 1)
```
entered over a block the same size as the input (Ctrl+Shift+Enter in older
Excel). Headings and labels pass through unchanged.

To name the totals yourself instead of relying on labels, call the core
directly — `rowParent(i)` is the row that claims row `i` as a component, or
`-1`:

```vba
Dim vals(0 To 3, 0 To 0) As Double, units() As Double
Dim rowParent(0 To 3) As Long, colParent(0 To 0) As Long
vals(0, 0) = 33.3: vals(1, 0) = 33.3: vals(2, 0) = 33.4: vals(3, 0) = 100
rowParent(0) = 3: rowParent(1) = 3: rowParent(2) = 3: rowParent(3) = -1
colParent(0) = -1
If FRRound(vals, 4, 1, rowParent, colParent, 1, 1, units) Then
    Debug.Print units(0, 0), units(1, 0), units(2, 0), units(3, 0)  ' 33 33 34 100
End If
Debug.Print FRViolations(units, 4, 1, rowParent, colParent)          ' 0
```

## How it works

Identical to the Python implementation, and the two agree figure for figure
(see *Testing* below). A block of rows against columns is a **flow network**: a
unit of flow from row *i* into column *j* is a unit in cell *(i, j)*,
conservation at the nodes *is* the totals footing, and arc bounds
`[floor, ceil]` are the "stay adjacent" rule. Network integrality guarantees a
consistent rounding exists; minimum cost picks the cheapest one. Nested
subtotals become a cascade of blocks solved outermost first, so the grand
total keeps its own nearest value and any compromise is pushed down to the
least aggregated figures.

`FinRound.bas` holds all of it — a min-cost circulation with lower bounds
(`NetSolve`), the block solver (`SolveBlock`), backwards total detection
(`FRDetect`) and the cascade (`FRRound`). It is plain Basic with no Excel
objects, so it also runs under LibreOffice.

Figures are measured in **ticks** — ten-thousandths of a rounding step — and
carried in `Double`, which holds whole numbers exactly up to 2^53. Every
rounding decision is therefore exact integer arithmetic, not floating point
comparison. If a table's figures are so large that this would stop being
exact, `FRRound` refuses and says to scale them down rather than returning
quietly wrong numbers.

## Testing, and what is not tested

`FinRound.bas` — the whole algorithm — **was executed and verified**: the five
`FRSelfTest` cases pass in a real Basic runtime, and on 40 randomly generated
tables it produced results **byte-identical to the Python implementation**,
with every row and column independently re-checked as footing.

`FinRoundExcel.bas` — reading ranges, writing the sheet, the shading — could
not be executed here, since that needs Excel itself. It was reviewed and
checked structurally, but treat the first run as a trial: `FRSelfTest` proves
the arithmetic, and the report on the output sheet re-derives every total from
the printed figures, so a problem in the front end would show up there rather
than pass silently.

## Limits

- Totals must appear **below** their components (and total columns to the
  right). A total written above its lines is not detected; name it yourself
  via `rowParent`.
- Derived figures — gross profit, ratios, per-share amounts — are differences
  and quotients, not sums. They round independently and are not forced into
  any total.
- If a stated total does **not** already foot, no rounding can preserve both it
  and its components. Such a total is left out of the structure and reported.
- Very large selections are refused; this is for statement tables, not whole
  sheets.
