"""Configuration that encodes the *logic* of the IFRS INR P&L check.

The check file reconciles a consolidated IFRS P&L report against the three
inputs it was built from.  Everything that is specific to *this* report — how
the columns are laid out, which P&L category is an income vs. an expense, and
where each category's local-currency figures should tie back to — lives here so
it can be reviewed and adjusted in one place without touching the engine.

Three reconciliations are performed (each expressed as a *difference* that
should be zero):

1. **LC tie-out** — every local-currency figure in the report must equal its
   source: income / tax / interest accounts tie to the *Real Time TB*; the
   functionally-split expense accounts tie to the relevant block of the
   *Aggregate Expenses* report.
2. **GC (INR) conversion** — each group-currency *Balance* figure must equal
   ``(LC Balance + LC Consol) x FX rate`` from the *MA Rates* table.
3. **GC consolidation** — each group-currency *Total* must equal
   ``GC Balance + Reclass + Elimination + Consol``.

Plus a net-profit reconciliation: the report's net profit must tie to the net
profit implied by the Real Time TB, and Income + Expense + Net Profit must net
to zero internally.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Sheet names used inside the generated check workbook -------------------
# These must match the names the formulas reference.
SHEET_CHECK = "Check"
SHEET_TB = "Real Time TB"
SHEET_AGG = "Aggregate Exp"
SHEET_RATES = "MA rates"

# --- Report layout ----------------------------------------------------------
# Row numbers (1-based) of the header band in the *input* IFRS P&L report.
REPORT_BLOCK_LABEL_ROW = 2     # "LC - Balance", "GC - Total", ...
REPORT_SUBHEADER_ROW = 3       # "BALSCH" / "BALSDE" / "BALSDK" / "Overall Result"
REPORT_ENTITY_NAME_ROW = 4     # "Base life science AG", ...
REPORT_CURRENCY_ROW = 5        # "CHF" / "EUR" / "DKK"
REPORT_FIRST_DATA_ROW = 6
REPORT_CATEGORY_COL = "A"      # P&L category (Revenue, Cost of Production, ...)
REPORT_ACCOUNT_COL = "B"       # GL account number
REPORT_DESC_COL = "C"          # GL description
REPORT_FIRST_NUMERIC_COL = "D"

# The label that marks an entity's "total of the block" column (no check).
OVERALL_LABEL = "Overall Result"

# The three column blocks that are reconciled, by their block label, and how.
#   "lc_source"  -> tie each entity figure back to TB / Aggregate Exp
#   "fx"         -> (LC Balance + LC Consol) * rate
#   "consol"     -> GC Balance + Reclass + Elimination + Consol
CHECK_LC_BALANCE = "LC - Balance"
CHECK_GC_BALANCE = "GC - Balance"
CHECK_GC_TOTAL = "GC - Total"

# Blocks summed for the GC-Balance FX check (everything before GC - Balance
# carrying a local-currency figure for the entity).
FX_SOURCE_BLOCKS = ("LC - Balance", "LC - Consol")
# Blocks summed for the GC-Total consolidation check.
CONSOL_SOURCE_BLOCKS = ("GC - Balance", "GC - Reclass", "GC - Elimination",
                        "GC - Consol")

# The full set of canonical block labels we recognise. A block label read from
# a report is matched to one of these ignoring spacing and case (so "GC-Total",
# "GC - Total" and "gc  -  total" are all treated as "GC - Total"), which keeps
# the checks working when the export's spelling varies slightly.
CANONICAL_BLOCKS = ("LC - Balance", "LC - Consol", "GC - Balance",
                    "GC - Reclass", "GC - Elimination", "GC - Consol",
                    "GC - Total")


def normalize_label(label) -> str:
    """Spacing- and case-insensitive key for matching block labels."""
    return "".join(str(label).split()).lower()


def canonical_block_label(label) -> str:
    """Map a raw block label to its canonical form when it matches one."""
    n = normalize_label(label)
    for c in CANONICAL_BLOCKS:
        if normalize_label(c) == n:
            return c
    return str(label).strip()


# --- Category mapping -------------------------------------------------------
@dataclass(frozen=True)
class CategoryRule:
    """How one P&L category behaves in the check.

    classification: top-level grouping used by the net-profit reconciliation
        ("Income", "Expense", "Net Profit", or "" to exclude, e.g. Minority
        Interest, which nets out and is not part of P&L).
    source: where the local-currency figures tie back to —
        "tb"             -> Real Time TB
        "<Agg block>"    -> a named block of the Aggregate Expenses report
        ""               -> no LC source tie-out (derived/net-profit lines)
    """

    classification: str
    source: str = ""


# Aggregate-Expenses block labels (the functional split of expenses).
AGG_COST = "Cost of revenue"
AGG_SALES = "Sales & Marketing"
AGG_GA = "General Administration"

# Default mapping for the Base life science consolidation P&L. It covers the
# category names used by *both* the IFRS INR and the Ind-AS Function-wise
# reports (the names differ but never clash), so the tool auto-handles either
# report. Categories are matched case-insensitively; unknown ones are reported,
# not guessed.
DEFAULT_CATEGORY_RULES: dict[str, CategoryRule] = {
    # --- IFRS INR section names ------------------------------------------
    "Revenue": CategoryRule("Income", "tb"),
    "Cost of Production": CategoryRule("Expense", AGG_COST),
    "Sales": CategoryRule("Expense", AGG_SALES),
    "General Administration": CategoryRule("Expense", AGG_GA),
    # --- Ind-AS Function-wise section names ------------------------------
    "Income": CategoryRule("Income", "tb"),
    "Software Development Exp": CategoryRule("Expense", AGG_COST),
    "Sales & Marketing Cost": CategoryRule("Expense", AGG_SALES),
    "Administration cost": CategoryRule("Expense", AGG_GA),
    # Depreciation is its own line in Ind-AS (full GL 290100); it ties to the
    # trial balance directly, like tax / interest.
    "Depreciation": CategoryRule("Expense", "tb"),
    # --- Ind-AS Nature-wise section names (no Aggregate Exp; all tie to TB) -
    "Employee Benefit Expenses": CategoryRule("Expense", "tb"),
    "Cost of Technical sub-contractors": CategoryRule("Expense", "tb"),
    "Travel expenses": CategoryRule("Expense", "tb"),
    "Software packages for own use": CategoryRule("Expense", "tb"),
    "Communication expenses": CategoryRule("Expense", "tb"),
    "Professional Charges": CategoryRule("Expense", "tb"),
    "Others": CategoryRule("Expense", "tb"),
    # --- common to both reports ------------------------------------------
    "Other Income": CategoryRule("Income", "tb"),
    "Provision for Tax": CategoryRule("Expense", "tb"),
    "Interest": CategoryRule("Expense", "tb"),
    "Interest Exp": CategoryRule("Expense", "tb"),
    "Interest Expense": CategoryRule("Expense", "tb"),
    "Finance Cost": CategoryRule("Expense", "tb"),
    "Finance Costs": CategoryRule("Expense", "tb"),
    "Provision for Investment": CategoryRule("Expense", "tb"),
    "Minority Interest": CategoryRule("", ""),  # nets out, excluded from P&L
    "Net Profit": CategoryRule("Net Profit", ""),
}

# Net-profit reconciliation classifications, in display order.
RECON_CLASSES = ("Income", "Expense", "Net Profit")


@dataclass
class CheckConfig:
    """Bundle of all tunables, so callers can override the defaults."""

    category_rules: dict[str, CategoryRule] = field(
        default_factory=lambda: dict(DEFAULT_CATEGORY_RULES)
    )
    # Absolute slack (in the figure's own units) before a difference is flagged.
    tolerance: float = 0.5
    # "P&L accounts" for the "Net Profit as per Real Time TB" sum are identified
    # by their leading digit — the 1-, 2- and 3-series accounts — which excludes
    # balance-sheet accounts (4-, 8-, 9-series). Using the leading digit (rather
    # than a numeric range) means it works whether the TB stores account numbers
    # as numbers OR as text (common in ERP exports), and regardless of how many
    # digits they have.
    pl_series: tuple = ("1", "2", "3")
    # The FX-rate VLOOKUP spans at least this many rows of the MA Rates table,
    # so additional currencies (the table can have a varying number) are never
    # left out of the lookup range.
    rate_lookup_rows: int = 150
    # GL account for "Dividend received" used in the Minority Interest sheet
    # (its GC-Balance figure per company feeds Profit-before-Dividend).
    dividend_account: int = 332010
    # Fallback rule for a category that isn't explicitly mapped. Used for the
    # Nature-wise report (no Aggregate Exp): the CLI sets this to
    # CategoryRule("Expense", "tb") when --agg is omitted, so every expense
    # nature ties straight to the trial balance. Left None for function-wise
    # reports, where an unmapped category is reported instead of guessed.
    default_rule: CategoryRule | None = None

    def rule_for(self, category: str) -> CategoryRule | None:
        if not category:
            return None
        for name, rule in self.category_rules.items():
            if name.strip().lower() == category.strip().lower():
                return rule
        return None

    def effective_rule(self, category: str) -> CategoryRule | None:
        """Explicit rule, or the default fallback (Nature-wise mode)."""
        if not category or not category.strip():
            return None
        return self.rule_for(category) or self.default_rule

    def is_pl_account(self, account) -> bool:
        if account is None:
            return False
        s = str(account).strip().lstrip("-")
        return bool(s) and s[0] in self.pl_series
