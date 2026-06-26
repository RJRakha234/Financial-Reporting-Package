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


def read_sheet(path: str, title: str, keep_formulas: bool = True) -> SheetData:
    """Copy an input sheet verbatim so it can be embedded in the check file."""
    wb = load_workbook(path, data_only=not keep_formulas)
    ws = wb.active
    cells: dict[str, object] = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None:
                cells[c.coordinate] = c.value
    return SheetData(title=title, cells=cells,
                     max_row=ws.max_row, max_col=ws.max_column)


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


def _find_cell_ci(ws, *names):
    """First cell whose stripped, lower-cased text equals one of ``names``."""
    wanted = {n.strip().lower() for n in names}
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.strip().lower() in wanted:
                return c.row, c.column
    return None


def parse_rates(path: str) -> RatesGeometry:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    # locate the currency-source ("From") and rate ("Exch. Rate") columns by
    # header text (case/spacing-tolerant), so the table can sit on any columns.
    from_rc = _find_cell_ci(ws, "From", "From Curr", "From Currency")
    rate_rc = _find_cell_ci(ws, "Exch. Rate", "Exchange Rate", "Exch Rate",
                            "Exchange rate", "Rate")
    from_col = from_rc[1] if from_rc else column_index_from_string("C")
    rate_col = rate_rc[1] if rate_rc else column_index_from_string("E")
    hdr_row = from_rc[0] if from_rc else 1
    rates: dict[str, float] = {}
    last = hdr_row
    for r in range(hdr_row + 1, ws.max_row + 1):
        cur = ws.cell(row=r, column=from_col).value
        rate = ws.cell(row=r, column=rate_col).value
        if isinstance(cur, str) and isinstance(rate, (int, float)):
            rates[cur.strip()] = float(rate)
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
