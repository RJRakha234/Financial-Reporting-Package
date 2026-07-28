"""finround — round a table without breaking its totals.

Rounding a financial table figure by figure is what makes published statements
fail to add up. ``finround`` rounds every cell of a multi-row, multi-column
table *at once*, so that each figure stays on one of the two multiples of the
step either side of its true value while every total, subtotal, row total,
column total and grand total still equals the sum of its rounded parts::

    from finround import round_table

    result = round_table(rows, scale=1000, decimals=1)
    print(result.report())
    result.values      # exact rounded figures
    result.consistent  # True: every total foots

Totals are inferred from the labels and the arithmetic, or given explicitly
with ``row_groups`` / ``col_groups``.
"""

from .report import to_dict, to_json
from .solver import RoundedTable, round_table
from .structure import Node, build_forest, detect_groups, is_total_label
from .table import Table
from .units import RoundingSpec, parse_value

__all__ = [
    "round_table",
    "RoundedTable",
    "Table",
    "RoundingSpec",
    "detect_groups",
    "build_forest",
    "is_total_label",
    "parse_value",
    "to_dict",
    "to_json",
    "Node",
]
