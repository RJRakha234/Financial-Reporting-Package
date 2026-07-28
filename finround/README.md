# finround — round a table without breaking its totals

Round a financial table figure by figure and it stops adding up. Three lines of
33.3 round to 33 each, but their total of 99.9 rounds to 100. Publish it and the
column is out by one; force the total to 99 instead and it disagrees with the
number everyone expects.

`finround` rounds **the whole table at once**. Every figure lands on one of the
two multiples of the rounding step either side of its true value, and *at the
same time* every total, subtotal, row total, column total and grand total is
exactly the sum of its rounded components — down the rows and across the
columns simultaneously, through nested subtotals, with negatives and blanks.

```
                   Q1      Q2      Q3      Q4  Total FY25
North            6.47    7.91    6.65   6.47*       27.50
South            7.22    8.28    2.95   2.87*      21.32*
East             7.27    6.79    8.85    8.64       31.55
West             2.90   1.67*    6.39    4.47       15.43
Total India     23.86   24.65  24.84*  22.45*      95.80*
Singapore        2.30    1.62    7.63    8.92       20.47
United Kingdom   0.96    8.39    5.72    6.48       21.55
Total overseas  3.26*   10.01   13.35   15.40       42.02
Total revenue   27.12  34.66*   38.19   37.85      137.82

Rounded to nearest 0.01, values divided by 10,000,000.
Structure: 3 row totals and 1 column total over 9x5 figures.
✓ all 24 total checks foot exactly after rounding.
9 of 45 figures were moved off their nearest value (marked *) so the table foots.
Cost of consistency: total movement 14.24 steps against 11.94 for straight
rounding; no figure moved more than 0.85 of a step.
```

Nine figures had to give way — and the report says exactly which, and by how
much, so nothing happens silently.

## Install

Pure standard library; nothing to install. `pytest` only to run the tests.

```bash
pip install -r requirements.txt   # optional: pytest
```

## Use it (command line)

```bash
# Rupees to crores, two decimals
python -m finround examples/segment_revenue.csv --scale 10000000 --decimals 2

# Write the result out, and machine-read it
python -m finround table.csv --scale 1000 -d 1 -o rounded.csv
python -m finround table.csv --json
```

The input CSV has a heading row and a label column by default (`--no-header`,
`--no-index` if not). The process exits `1` if any constraint could not be met,
so it drops straight into a pipeline.

| Option | What it does |
| --- | --- |
| `--scale N` | divide every figure by `N` first (1000 for thousands, 10000000 for crores) |
| `-d, --decimals N` | decimal places to present |
| `--step X` | round to a multiple of `X` instead — `0.5`, `25`, `1000` |
| `--total-rows 5,9` | 1-based rows that are totals, instead of guessing from labels |
| `--total-cols 6` | likewise for columns |
| `--no-detect` | ignore totals entirely and round each figure independently |
| `--tolerance T` | accept a stated total that only foots to within `T` |
| `-o FILE` `--json` `--quiet` | write CSV / print JSON / print just the table |

## Use it (library)

```python
from finround import round_table, Table

table = Table.from_csv("segments.csv")
result = round_table(table, scale=1000, decimals=1)

result.values          # exact rounded figures (Fractions)
result.formatted()     # ["1,234.6", ...] ready to print
result.consistent      # True — every total foots
result.violations()    # [] — and here is the proof
result.adjusted_cells()  # the figures that had to give way
print(result.report())
```

Figures may be numbers or strings as they appear in a statement — `1,234`,
`(1,234)`, `₹ 45,000`, `1 234 567`, a bare `–` for nil. Everything is carried as
exact rational arithmetic, never binary floats, so "does this foot?" always has
a definite answer.

If the labels do not give the structure away, state it:

```python
round_table(table, row_groups={4: [0, 1, 2, 3], 7: [5, 6], 8: [4, 7]})
round_table(table, row_totals=[4, 7, 8])   # members inferred, totals given
```

## How it works

1. **Structure** (`structure.py`) — a total is matched *backwards*: for a row
   that reads like a total, the longest trailing block of preceding rows that
   adds up to it **in every column at once** becomes its components. Matched
   blocks roll up, so an outer total sums subtotals rather than raw lines and
   nesting works to any depth. A "total" that nothing adds up to is reported,
   never invented.

2. **Rounding** (`solver.py`, `flow.py`) — a block of rows against columns is
   exactly a **flow network**: a unit of flow from row *i* into column *j* is a
   unit in cell *(i, j)*, conservation at the nodes *is* the totals footing, and
   arc bounds `[floor, ceil]` are the "stay adjacent" rule. Two classical facts
   do the work:

   * the constraint matrix of a network is totally unimodular, so an integral
     solution exists whenever a fractional one does — and the un-rounded table
     is a fractional one. **A consistent rounding is therefore guaranteed, not
     hoped for**;
   * minimising a linear cost over that network yields the *cheapest* such
     rounding, so no figure moves further than it must. Absolute deviation is
     modelled as one unit-capacity arc per step at an increasing price, which
     keeps the cost convex and exactly linear-programmable.

   Nested subtotals turn the single network into a cascade of blocks, solved
   **outermost first**: the grand total keeps its own nearest value, then the
   totals, then the detail. Whatever an outer block decided is fixed when the
   block refining it is solved, so every figure is decided exactly once and
   consistency holds by construction rather than being patched up afterwards.
   The deliberate consequence is that **compromise is pushed down to the least
   aggregated figures**, which is where a reader tolerates it.

3. **Reporting** (`report.py`) — which figures moved, in which direction, the
   total movement against a straight rounding, and every constraint re-checked
   on the output. `result.violations()` re-derives the sums from the printed
   numbers rather than trusting the solver.

## Guarantees, and their edges

For any table whose stated totals genuinely foot, the output satisfies:

- every total equals the sum of its rounded components, in both dimensions;
- every figure is within one rounding step of its true value (`max_deviation()`
  is always `< 1`);
- the grand total, and each margin in turn, keeps its own nearest value;
- within each block, total movement is the minimum possible — verified in the
  tests against brute-force enumeration of every valid alternative.

Where it stops short, and says so:

- **Totals that do not already foot.** If a stated total is out before
  rounding, no rounding can preserve both it and its components. `finround`
  leaves it out of the structure and warns — or, with `--tolerance`, makes it
  foot and tells you the total may shift. (Use
  [`fincheck`](../fincheck) to find such breaks in a PDF statement first.)
- **Derived figures.** Gross profit, ratios and per-share figures are
  differences and quotients, not sums; they are rounded independently and are
  not forced into any total.
- **Totals written above their components**, and any structure the labels and
  the arithmetic do not reveal, have to be given as `row_groups` /
  `col_groups`; detection only ever looks backwards from a total.
- **Deeply nested tables in both dimensions at once** can, in principle, run
  out of room once several levels of margins are fixed. Rather than silently
  breaking a total, `finround` widens the allowance a step at a time, reports
  how much leeway it needed (`slack_used`), and still returns a table that
  foots.

## Tests

```bash
python -m pytest tests -q      # 150+ tests
```

Beyond unit tests, the suite includes randomised property tests over a hundred
tables — with negatives, nested hierarchies, awkward denominators and shapes
from a single cell up to 26×31 — asserting on every one that the result foots
and that no figure moved a whole step, plus brute-force checks that no cheaper
valid rounding exists.
