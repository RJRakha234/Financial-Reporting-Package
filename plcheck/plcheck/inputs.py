"""Read the input workbooks: the IFRS P&L report and its three sources."""

from __future__ import annotations

from dataclasses import dataclass, field

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter

from . import config as C
from .model import Block, Entity, ReportRow, ReportTable, SheetData


def _consecutive_blocks(ws, label_row: int, first_col: str, last_col: int):
    """Group columns into blocks of identical block-label (row ``label_row``)."""
    blocks: list[tuple[str, list[int]]] = []
    start = column_index_from_string(first_col)
    for col in range(start, last_col + 1):
        label = ws.cell(row=label_row, column=col).value
        if label is None:
            continue
        if blocks and blocks[-1][0] == label:
            blocks[-1][1].append(col)
        else:
            blocks.append((str(label).strip(), [col]))
    return blocks


def _find_block_label_row(ws, max_scan: int = 12) -> int:
    """Row holding the column-block labels (e.g. 'LC - Balance').

    The header band can start on row 1 or row 2 depending on the export, so we
    locate it rather than assume a fixed row.
    """
    targets = {C.normalize_label(b) for b in C.CANONICAL_BLOCKS}
    for r in range(1, max_scan + 1):
        for cell in ws[r]:
            if isinstance(cell.value, str) and C.normalize_label(cell.value) in targets:
                return r
    return C.REPORT_BLOCK_LABEL_ROW


def read_report(path: str) -> ReportTable:
    """Parse the IFRS / Ind-AS P&L report into a :class:`ReportTable`.

    The header band is located by *label* (and so is each block), and its rows
    are derived from the block-label row, so reports that start the header on
    row 1 or row 2 - with any number of blocks / entities / accounts - are all
    handled.
    """
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    last_col = ws.max_column

    # --- locate the header band --------------------------------------------
    block_row = _find_block_label_row(ws)
    sub_row = block_row + 1
    name_row = block_row + 2
    ccy_row = block_row + 3
    first_data_row = block_row + 4

    # --- entities (from the sub-header / name / currency rows) --------------
    block_groups = _consecutive_blocks(
        ws, block_row, C.REPORT_FIRST_NUMERIC_COL, last_col
    )

    blocks: list[Block] = []
    entities: list[Entity] = []
    seen_codes: set[str] = set()
    for label, cols in block_groups:
        colmap: dict[str, str] = {}
        for col in cols:
            sub = ws.cell(row=sub_row, column=col).value
            if sub is None:
                continue
            sub = str(sub).strip()
            colmap[sub] = get_column_letter(col)
            if sub != C.OVERALL_LABEL and sub not in seen_codes:
                seen_codes.add(sub)
                entities.append(
                    Entity(
                        code=sub,
                        name=str(ws.cell(row=name_row,
                                         column=col).value or "").strip(),
                        currency=str(ws.cell(row=ccy_row,
                                             column=col).value or "").strip(),
                    )
                )
        blocks.append(Block(label=C.canonical_block_label(label),
                            columns=colmap))

    # --- data rows ---------------------------------------------------------
    cat_c = column_index_from_string(C.REPORT_CATEGORY_COL)
    acct_c = column_index_from_string(C.REPORT_ACCOUNT_COL)
    desc_c = column_index_from_string(C.REPORT_DESC_COL)

    rows: list[ReportRow] = []
    errors: list[str] = []
    for r in range(first_data_row, ws.max_row + 1):
        category = ws.cell(row=r, column=cat_c).value
        account = ws.cell(row=r, column=acct_c).value
        desc = ws.cell(row=r, column=desc_c).value
        # carry the category forward onto subtotal rows that leave A blank
        if (category is None and account is None
                and not any(ws.cell(row=r, column=column_index_from_string(c)).value
                            for b in blocks for c in b.columns.values())):
            continue
        values: dict[tuple[str, str], float] = {}
        for b in blocks:
            for sub, col in b.columns.items():
                v = ws.cell(row=r, column=column_index_from_string(col)).value
                if isinstance(v, str) and v.startswith("#"):
                    errors.append(
                        f"{str(category).strip() if category else ''} / {b.label}"
                        f" / {sub}: {v.strip()} (cell {col}{r})")
                    v = 0.0
                values[(b.label, sub)] = float(v) if isinstance(v, (int, float)) else 0.0
        rows.append(
            ReportRow(
                category=str(category).strip() if category else "",
                account=account,
                description=str(desc).strip() if desc else "",
                values=values,
            )
        )

    if not entities or not blocks:
        raise ValueError(
            f"Could not recognise the report layout in '{path}'. Expected block "
            f"headings (e.g. 'LC - Balance') in row {C.REPORT_BLOCK_LABEL_ROW} "
            f"and entity codes (e.g. 'BALSCH') in row {C.REPORT_SUBHEADER_ROW}. "
            "Check that the first worksheet is the P&L report and that its "
            "header rows match the expected layout.")

    return ReportTable(entities=entities, blocks=blocks, rows=rows, errors=errors)


def read_sheet(path: str, title: str, keep_formulas: bool = True,
               sheet_name: str | None = None) -> SheetData:
    """Copy an input sheet verbatim so it can be embedded in the check file."""
    wb = load_workbook(path, data_only=not keep_formulas)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    wsv = None
    if keep_formulas:
        # a second, values-only view of the same sheet, used to substitute the
        # calculated value for any formula that cannot travel into the check
        # file (see below).
        wsv = load_workbook(path, data_only=True)[ws.title]
    cells: dict[str, object] = {}
    text_coords: set = set()
    for row in ws.iter_rows():
        for c in row:
            if c.value is None:
                continue
            v = c.value
            if c.data_type == "f":
                f = v if isinstance(v, str) else getattr(v, "text", None) or ""
                # A formula that reaches outside its own sheet ('Other tab'!A1
                # or [Book1.xlsx]...) would arrive dangling in the check file
                # (its target isn't embedded) and Excel would offer to "repair"
                # the workbook. Embed its calculated value instead. Non-string
                # formula objects (array formulas etc.) get the same treatment.
                if "!" in f or "[" in f or not isinstance(v, str):
                    v = wsv.cell(row=c.row, column=c.column).value if wsv else None
                    if v is None:
                        continue
            # a *text* cell whose content starts with "=" (e.g. a note typed
            # as "=Correction of ...") must stay text when embedded; written
            # as-is openpyxl would store it as a broken formula and Excel
            # would flag the workbook for repair.
            elif (c.data_type == "s" and isinstance(v, str)
                    and v.lstrip().startswith("=")):
                text_coords.add(c.coordinate)
            cells[c.coordinate] = v
    return SheetData(title=title, cells=cells,
                     max_row=ws.max_row, max_col=ws.max_column,
                     text_coords=text_coords)


# --- Geometry of the source sheets -----------------------------------------
# These describe *where* things are so the generated formulas (and the Python
# evaluator) adapt to a revised file with more/fewer rows or accounts.

def account_key(value):
    """Canonical key for a GL account, tolerant of text-formatted numbers.

    ERP exports often store account numbers as text ("110200"); this returns an
    int for numeric or all-digit values so the report and TB/Aggregate match
    regardless of how each stored them. Non-account cells return None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    return int(s) if s.isdigit() else None


def _find_cell(ws, text):
    """Return (row, col) of the first cell whose stripped text == ``text``."""
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.strip() == text:
                return c.row, c.column
    return None


def _find_header_row(ws, codes):
    """First row that contains any of the entity ``codes`` (case-insensitive)."""
    code_set = {str(c).strip().lower() for c in codes}
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.strip().lower() in code_set:
                return c.row
    return None


@dataclass
class TBGeometry:
    header_row: int            # row carrying BALSCH / BALSDE / BALSDK
    first_col: int             # first column of the used range (for MATCH)
    last_col: int              # last column (last entity)
    first_data_row: int        # first row that holds a GL account
    last_data_row: int         # last row to include in VLOOKUP table
    sum_row: int               # the workbook's own SUM row (not relied upon)
    entity_first_col: int      # column of the first entity code in header_row
    acct_col: int = 1          # the "Group Account Number" column
    entity_cols: dict[str, int] = field(default_factory=dict)  # code -> col


def _entity_columns(ws, header_row, codes) -> dict:
    """Map each report entity code to its column on ``header_row`` in this sheet,
    matched case-insensitively (keyed by the report's spelling of the code)."""
    by_norm = {str(c).strip().lower(): c for c in codes}
    out: dict[str, int] = {}
    for cell in ws[header_row]:
        if isinstance(cell.value, str):
            rep = by_norm.get(cell.value.strip().lower())
            if rep is not None:
                out.setdefault(rep, cell.column)
    return out


def parse_tb(path: str, codes) -> TBGeometry:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    hdr = _find_header_row(ws, codes) or C.REPORT_FIRST_DATA_ROW
    ent_cols = _entity_columns(ws, hdr, codes)
    acct_rc = _find_cell(ws, "Group Account Number")
    acct_col = acct_rc[1] if acct_rc else 1
    # first/last row that actually carries a GL account (skips the text header
    # band so a SUMPRODUCT over the account column never hits stray text).
    gl_rows = [r for r in range(hdr + 1, ws.max_row + 1)
               if account_key(ws.cell(row=r, column=acct_col).value) is not None]
    return TBGeometry(
        header_row=hdr,
        first_col=1,
        last_col=ws.max_column,
        first_data_row=gl_rows[0] if gl_rows else hdr + 1,
        last_data_row=gl_rows[-1] if gl_rows else ws.max_row,
        sum_row=ws.max_row,
        entity_first_col=min(ent_cols.values()) if ent_cols else 1,
        acct_col=acct_col,
        entity_cols=ent_cols,
    )


@dataclass
class AggBlock:
    label: str
    acct_col: int
    subheader_row: int
    first_data_row: int
    last_data_row: int
    entity_cols: dict[str, int] = field(default_factory=dict)  # code -> col
    last_entity_col: int = 0


@dataclass
class AggGeometry:
    blocks: dict[str, AggBlock]       # block label -> AggBlock
    match_first_col: int              # first block's acct col (for MATCH range)
    match_last_col: int               # first block's last entity col
    subheader_row: int


def parse_agg(path: str, codes) -> AggGeometry:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    code_set = set(codes)
    sub_row = _find_header_row(ws, codes)
    label_row = sub_row - 1          # block labels sit just above the sub-header

    # account columns: each functional block has its own "Group Account Number"
    acct_cols = {
        c.column for row in ws.iter_rows() for c in row
        if isinstance(c.value, str) and c.value.strip() == "Group Account Number"
    }

    # Bound each block by the *label row* groups, so a block's entity columns
    # never bleed into the neighbouring block (e.g. the column-less "Total").
    groups = _consecutive_blocks(ws, label_row,
                                 get_column_letter(1), ws.max_column)
    blocks: dict[str, AggBlock] = {}
    for label, cols in groups:
        ents = {ws.cell(row=sub_row, column=col).value.strip(): col
                for col in cols
                if isinstance(ws.cell(row=sub_row, column=col).value, str)
                and ws.cell(row=sub_row, column=col).value.strip() in code_set}
        if not ents:
            continue
        # the block's account column is the nearest "Group Account Number" to
        # the left of its first entity column
        candidates = [a for a in acct_cols if a < min(ents.values())]
        if not candidates:
            continue                 # e.g. the "Total" block has no account col
        ac = max(candidates)
        data_rows = [r for r in range(sub_row + 1, ws.max_row + 1)
                     if account_key(ws.cell(row=r, column=ac).value) is not None]
        blocks[label] = AggBlock(
            label=label, acct_col=ac, subheader_row=sub_row,
            first_data_row=min(data_rows) if data_rows else sub_row + 1,
            last_data_row=max(data_rows) if data_rows else ws.max_row,
            entity_cols=ents, last_entity_col=max(ents.values()),
        )
    if not blocks:
        raise ValueError(
            f"Could not recognise any expense blocks in the Aggregate Expenses "
            f"file '{path}'. Expected functional blocks (e.g. 'Cost of revenue') "
            "each with a 'Group Account Number' column and entity sub-columns.")
    first = blocks[next(iter(blocks))]
    return AggGeometry(blocks=blocks, match_first_col=first.acct_col,
                       match_last_col=first.last_entity_col, subheader_row=sub_row)


@dataclass
class RatesGeometry:
    from_col: int
    rate_col: int
    first_row: int
    last_row: int
    rates: dict[str, float] = field(default_factory=dict)   # currency -> rate

    @property
    def col_index(self) -> int:
        return self.rate_col - self.from_col + 1


def _is_currency_code(v) -> bool:
    """True for a 3-letter currency code (CHF, INR, USD …) — text-only."""
    s = str(v).strip()
    return len(s) == 3 and s.isalpha()


def _norm_hdr(v) -> str:
    return " ".join(str(v).replace(".", " ").split()).lower()


def _as_number(v):
    """Float from a numeric cell OR a numeric-looking text cell ('67.81',
    '1,234.5') — ERP exports often store rates as text, which Excel happily
    multiplies but a strict isinstance check would reject."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def _is_rate_header(v) -> bool:
    """True for an *exchange-rate* header, excluding 'From Ratio' / 'Rate Type'."""
    n = _norm_hdr(v)
    if not n or "ratio" in n or "type" in n:
        return False
    return n in ("exchange rate", "exch rate", "rate") or "exch" in n


def _col_has_decimals(ws, r1: int, r2: int, c: int) -> bool:
    """True if the column holds a non-integer number (a rate, not a date/ratio);
    text-formatted numbers ('67.81') count too."""
    for r in range(r1, r2 + 1):
        v = _as_number(ws.cell(row=r, column=c).value)
        if v is not None and v != int(v):
            return True
    return False


def parse_rates(path: str) -> RatesGeometry:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    # Locate the columns by their *data*, so it is immune to the number of
    # columns and their header wording:
    #   - the "From" currency column is the FIRST column whose cells are
    #     3-letter currency codes (the "To" column, all "INR", comes later);
    #   - the rate column is the one headed like an exchange rate, or failing
    #     that the first column to the right holding decimal numbers (a date or
    #     a 1/100 "From Ratio" column is all integers, so it is skipped).
    scan_last = min(ws.max_row, 80)
    from_col = 0
    first_code_row = 2
    for c in range(1, ws.max_column + 1):
        for r in range(1, scan_last + 1):
            if _is_currency_code(ws.cell(row=r, column=c).value):
                from_col, first_code_row = c, r
                break
        if from_col:
            break
    if not from_col:
        from_col, first_code_row = column_index_from_string("C"), 2
    hdr_row = max(1, first_code_row - 1)

    rate_col = 0
    for c in range(from_col, ws.max_column + 1):
        if _is_rate_header(ws.cell(row=hdr_row, column=c).value):
            rate_col = c
            break
    if not rate_col:
        for c in range(from_col + 1, ws.max_column + 1):
            if _col_has_decimals(ws, hdr_row + 1, min(ws.max_row, hdr_row + 80), c):
                rate_col = c
                break
    if not rate_col:
        rate_col = from_col + 3

    rates: dict[str, float] = {}
    last = hdr_row
    for r in range(hdr_row + 1, ws.max_row + 1):
        cur = ws.cell(row=r, column=from_col).value
        rate = _as_number(ws.cell(row=r, column=rate_col).value)
        if isinstance(cur, str) and rate is not None:
            rates[cur.strip()] = rate
            last = r
    return RatesGeometry(from_col=from_col, rate_col=rate_col,
                         first_row=hdr_row + 1, last_row=last, rates=rates)


# --- Value extraction (for the independent Python evaluator) ----------------

def tb_values(path: str, codes):
    """Return ({account: {code: value}}, {code: net-profit sum})."""
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    hdr = _find_header_row(ws, codes)
    code_col = _entity_columns(ws, hdr, codes)
    acct_rc = _find_cell(ws, "Group Account Number")
    acct_col = acct_rc[1] if acct_rc else 1
    accounts: dict[object, dict[str, float]] = {}
    for r in range(hdr + 1, ws.max_row + 1):
        key = account_key(ws.cell(row=r, column=acct_col).value)
        if key is None:
            continue
        accounts[key] = {
            code: float(ws.cell(row=r, column=col).value or 0)
            for code, col in code_col.items()
        }
    sums = {code: float(ws.cell(row=ws.max_row, column=col).value or 0)
            for code, col in code_col.items()}
    return accounts, sums


def agg_values(path: str, codes):
    """Return {block_label: {account: {code: value}}}."""
    geo = parse_agg(path, codes)
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    out: dict[str, dict[object, dict[str, float]]] = {}
    for label, blk in geo.blocks.items():
        table: dict[object, dict[str, float]] = {}
        for r in range(blk.first_data_row, blk.last_data_row + 1):
            key = account_key(ws.cell(row=r, column=blk.acct_col).value)
            if key is None:
                continue
            table[key] = {code: float(ws.cell(row=r, column=col).value or 0)
                          for code, col in blk.entity_cols.items()}
        out[label] = table
    return out


def detect_gc_currency(report, rates: dict, default: str = "INR") -> str:
    """Infer the group/consolidation currency of the report.

    The MA Rates table is quoted to INR, so for any row
        GC-Balance = (LC-Balance + LC-Consol) * (local->INR) / (GC->INR)
    => GC->INR = (LC + Consol) * local-rate / GC-Balance.  Match that implied
    INR rate to a currency in the table (the mode across rows wins), so an IFRS
    INR report resolves to INR and an IFRS USD report to USD - no flag needed.
    """
    from . import config as C
    votes: dict[str, int] = {}
    for row in report.rows:
        for e in report.entities:
            lc = (row.values.get((C.CHECK_LC_BALANCE, e.code), 0.0)
                  + row.values.get(("LC - Consol", e.code), 0.0))
            gc = row.values.get((C.CHECK_GC_BALANCE, e.code), 0.0)
            lc_rate = rates.get(e.currency)
            if not lc or not gc or not lc_rate:
                continue
            implied = lc * lc_rate / gc                      # GC -> INR
            cur, err = None, None
            for c, rate in rates.items():
                if rate:
                    rel = abs(rate - implied) / rate
                    if err is None or rel < err:
                        cur, err = c, rel
            if cur is not None and err is not None and err < 0.01:
                votes[cur] = votes.get(cur, 0) + 1
    if not votes:
        return default
    return max(votes, key=votes.get)


# --- Consolidation entry tracker (LC - Consol tie-out) ----------------------

@dataclass
class ConsolGeometry:
    """Where the columns sit in the manual consolidation-entry tracker.

    The tracker is a separate workbook ("Consolidation entries …"). Each manual
    entry is a Dr/Cr pair of rows; the figure that lands in the report's
    LC - Consol block is the P&L leg's amount for the latest month. We locate
    everything by header/data so it is immune to column count and wording.
    """
    sheet_name: str
    concat_col: int               # "Concatenate" = comp-code + account (join key)
    val_cols: tuple               # the latest month's two value columns (Dr/Cr)
    first_row: int                # first data row
    last_row: int
    func_col: int = 0             # "Functional Group" column (0 if absent)
    totals: dict = field(default_factory=dict)        # concat -> net amount
    # (concat, functional-code) -> net amount, for the function-wise split
    totals_fn: dict = field(default_factory=dict)
    # concat -> (comp_code, account, description), for the completeness check
    key_info: dict = field(default_factory=dict)


def propagate_func_column(ws, concat_col, func_col, dr_col, cr_col,
                          first_row, last_row, tol: float = 0.5) -> None:
    """Fill the Functional Group code across both legs of each entry, in place.

    An entry's COS/S&M/G&A code may be written on either its P&L leg or its
    contra leg, so the blank cells must inherit it. Entries are delimited
    robustly by **Debit = Credit balance**: legs are accumulated until the
    running Debit equals the running Credit (a complete entry), then closed.
    This separates even back-to-back entries with no blank row between them, so
    a code can't bleed from one entry to the next. A row with no Concatenate
    (blank or narration) also closes the current entry.
    """
    if not func_col:
        return

    def num(r, c):
        if not c:
            return 0.0
        # text-formatted amounts count too (VBA coerces them silently, so the
        # two editions must group entries identically)
        v = _as_number(ws.cell(row=r, column=c).value)
        return v if v is not None else 0.0

    def close(rows):
        gfunc = ""
        for rr in rows:
            v = ws.cell(row=rr, column=func_col).value
            if v is not None and str(v).strip():
                gfunc = str(v).strip()
                break
        if gfunc:
            for rr in rows:
                v = ws.cell(row=rr, column=func_col).value
                if v is None or str(v).strip() == "":
                    ws.cell(row=rr, column=func_col, value=gfunc)

    rows, sum_dr, sum_cr = [], 0.0, 0.0
    for r in range(first_row, last_row + 1):
        concat = str(ws.cell(row=r, column=concat_col).value or "").strip()
        if not concat:                       # blank / narration ends the entry
            if rows:
                close(rows)
            rows, sum_dr, sum_cr = [], 0.0, 0.0
            continue
        rows.append(r)
        sum_dr += num(r, dr_col)
        sum_cr += num(r, cr_col)
        if abs(sum_dr - sum_cr) <= tol and (abs(sum_dr) > tol or abs(sum_cr) > tol):
            close(rows)                      # balanced -> a complete entry
            rows, sum_dr, sum_cr = [], 0.0, 0.0
    if rows:
        close(rows)


def _find_consol_sheet(wb):
    """The worksheet that holds the tracker (has a 'Concatenate' header)."""
    for ws in wb.worksheets:
        for r in range(1, min(ws.max_row, 40) + 1):
            for c in range(1, min(ws.max_column, 40) + 1):
                if _norm_hdr(ws.cell(row=r, column=c).value) == "concatenate":
                    return ws, r, c
    return wb.active, 1, 0


def parse_consol(path: str) -> ConsolGeometry:
    wb = load_workbook(path, data_only=True)
    ws, hdr_row, concat_col = _find_consol_sheet(wb)
    if not concat_col:
        raise ValueError("consol tracker: no 'Concatenate' column found")

    # right boundary of the value area = just left of the 'Currency'/'Type' cols
    boundary = ws.max_column
    for c in range(concat_col + 1, ws.max_column + 1):
        if _norm_hdr(ws.cell(row=hdr_row, column=c).value) in ("currency", "type"):
            boundary = c - 1
            break

    # the "Functional Group" column (COS / S&M / G&A), if present
    func_col = 0
    for c in range(1, ws.max_column + 1):
        if _norm_hdr(ws.cell(row=hdr_row, column=c).value) in (
                "functional group", "function group", "functional grp"):
            func_col = c
            break

    # columns used for the completeness check (locate by header, optional)
    def _find_hdr(*names):
        wanted = set(names)
        for c in range(1, ws.max_column + 1):
            if _norm_hdr(ws.cell(row=hdr_row, column=c).value) in wanted:
                return c
        return 0
    acct_col = _find_hdr("group account number", "group account no",
                         "group acct number", "account", "gl account", "glaccount")
    comp_col = _find_hdr("comp code", "company code", "compcode", "comp. code")
    desc_col = _find_hdr("gl descriptions", "gl description", "description",
                         "gl desc")

    last_row = hdr_row
    for r in range(hdr_row + 1, ws.max_row + 1):
        if str(ws.cell(row=r, column=concat_col).value or "").strip():
            last_row = r
    first_row = hdr_row + 1

    # value columns = the latest month's Debit/Credit pair, i.e. the two
    # right-most columns of the value area (just left of Currency/Type). Earlier
    # columns (Group Account Number, prior months) are never the right-most
    # pair, so a numeric account-number column can't be mistaken for a value.
    # The amount we want is the NET posting to the account = Debit - Credit:
    # each manual entry is a Dr leg (charge, in the left column) and a "To ..."
    # Cr leg (in the right column); the report's LC - Consol carries the credit
    # legs as negative, so the credit column must be SUBTRACTED, not added
    # (adding it would double the difference).
    if boundary - 1 > concat_col:
        val_cols = (boundary - 1, boundary)         # (Debit, Credit)
    else:
        val_cols = (boundary,)

    # carry each entry's functional code onto both its legs (it may be typed on
    # the P&L leg or the contra leg), so the match works on the P&L leg's row.
    dr_col = val_cols[0]
    cr_col = val_cols[1] if len(val_cols) > 1 else 0
    propagate_func_column(ws, concat_col, func_col, dr_col, cr_col,
                          first_row, last_row)

    def _num(r, c):
        v = _as_number(ws.cell(row=r, column=c).value)
        return v if v is not None else 0.0

    def _cell(r, c):
        return str(ws.cell(row=r, column=c).value or "").strip() if c else ""

    totals: dict[str, float] = {}
    totals_fn: dict[tuple, float] = {}
    key_info: dict = {}
    for r in range(first_row, last_row + 1):
        key = str(ws.cell(row=r, column=concat_col).value or "").strip()
        if not key:
            continue
        amt = _num(r, val_cols[0])
        if len(val_cols) > 1:
            amt -= _num(r, val_cols[1])             # Debit - Credit
        totals[key] = totals.get(key, 0.0) + amt    # account-only (nature-wise)
        if func_col:                                # account + functional group
            fc = str(ws.cell(row=r, column=func_col).value or "").strip().upper()
            totals_fn[(key, fc)] = totals_fn.get((key, fc), 0.0) + amt
        if key not in key_info and acct_col:
            comp = _cell(r, comp_col)
            acct = _cell(r, acct_col)
            if not comp and acct and key.upper().endswith(acct.upper()):
                comp = key[:len(key) - len(acct)]   # derive comp from Concatenate
            key_info[key] = (comp, acct, _cell(r, desc_col))

    return ConsolGeometry(sheet_name=ws.title, concat_col=concat_col,
                          val_cols=val_cols, func_col=func_col,
                          totals_fn=totals_fn, key_info=key_info,
                          first_row=first_row, last_row=last_row, totals=totals)


# Labels that sit among the company codes but are NOT codes themselves.
_NON_CODE = {"company", "overall result", "group account number",
             "consolidation unit", "group", "total", "result", "unit",
             "gl description | currency"}
# space-stripped, lower-cased forms, so the match tolerates any casing/spacing
# (e.g. "Consolidation Unit", "CONSOLIDATION  UNIT").
_NON_CODE_NORM = {"".join(w.split()).lower() for w in _NON_CODE}


def looks_like_company_code(value) -> bool:
    """True for a real company code: short, alphanumeric, no spaces — so a
    dimension label like 'Consolidation unit' is rejected (matched tolerant of
    spacing and upper/lower case)."""
    s = str(value).strip()
    if "".join(s.split()).lower() in _NON_CODE_NORM:
        return False
    return 2 <= len(s) <= 15 and " " not in s and s.isalnum()


def _codes_after_company(ws) -> list:
    """Company codes in a sheet: the cells to the right of each 'Company'
    label, up to a blank or 'Overall Result'. Works for the TB (one header) and
    the Aggregate Exp (one 'Company' per functional block). De-duplicated."""
    out: list = []
    for row in ws.iter_rows():
        cells = list(row)
        for i, c in enumerate(cells):
            if isinstance(c.value, str) and c.value.strip().lower() == "company":
                for c2 in cells[i + 1:]:
                    s = "" if c2.value is None else str(c2.value).strip()
                    if not s or s.lower() == "overall result":
                        break
                    if looks_like_company_code(s):
                        out.append(s)
    seen, res = set(), []
    for s in out:
        if s.upper() not in seen:
            seen.add(s.upper())
            res.append(s)
    return res


def company_codes(path: str) -> list:
    """All company codes present in a TB / Aggregate-Expenses workbook."""
    wb = load_workbook(path, data_only=True)
    return _codes_after_company(wb.active)


def consol_key(code, account) -> str:
    """Join key matching the tracker's 'Concatenate' (comp-code + account)."""
    a = account
    if isinstance(a, float) and a.is_integer():
        a = int(a)
    return f"{code}{a}".strip()
