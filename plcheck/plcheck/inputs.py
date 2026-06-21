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


def read_report(path: str) -> ReportTable:
    """Parse the IFRS P&L report into a :class:`ReportTable`.

    The header band is read by *label*, not by fixed column letters, so extra
    blocks / entities / accounts in a revised file are picked up automatically.
    """
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    last_col = ws.max_column

    # --- entities (from the sub-header / name / currency rows) --------------
    block_groups = _consecutive_blocks(
        ws, C.REPORT_BLOCK_LABEL_ROW, C.REPORT_FIRST_NUMERIC_COL, last_col
    )

    blocks: list[Block] = []
    entities: list[Entity] = []
    seen_codes: set[str] = set()
    for label, cols in block_groups:
        colmap: dict[str, str] = {}
        for col in cols:
            sub = ws.cell(row=C.REPORT_SUBHEADER_ROW, column=col).value
            if sub is None:
                continue
            sub = str(sub).strip()
            colmap[sub] = get_column_letter(col)
            if sub != C.OVERALL_LABEL and sub not in seen_codes:
                seen_codes.add(sub)
                entities.append(
                    Entity(
                        code=sub,
                        name=str(ws.cell(row=C.REPORT_ENTITY_NAME_ROW,
                                         column=col).value or "").strip(),
                        currency=str(ws.cell(row=C.REPORT_CURRENCY_ROW,
                                             column=col).value or "").strip(),
                    )
                )
        blocks.append(Block(label=label, columns=colmap))

    # --- data rows ---------------------------------------------------------
    cat_c = column_index_from_string(C.REPORT_CATEGORY_COL)
    acct_c = column_index_from_string(C.REPORT_ACCOUNT_COL)
    desc_c = column_index_from_string(C.REPORT_DESC_COL)

    rows: list[ReportRow] = []
    for r in range(C.REPORT_FIRST_DATA_ROW, ws.max_row + 1):
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
                values[(b.label, sub)] = float(v) if isinstance(v, (int, float)) else 0.0
        rows.append(
            ReportRow(
                category=str(category).strip() if category else "",
                account=account,
                description=str(desc).strip() if desc else "",
                values=values,
            )
        )

    return ReportTable(entities=entities, blocks=blocks, rows=rows)


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

def _find_cell(ws, text):
    """Return (row, col) of the first cell whose stripped text == ``text``."""
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.strip() == text:
                return c.row, c.column
    return None


def _find_header_row(ws, codes):
    """First row that contains any of the entity ``codes``."""
    code_set = {c for c in codes}
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.strip() in code_set:
                return c.row
    return None


@dataclass
class TBGeometry:
    header_row: int            # row carrying BALSCH / BALSDE / BALSDK
    first_col: int             # first column of the used range (for MATCH)
    last_col: int              # last column (last entity)
    last_data_row: int         # last row to include in VLOOKUP table
    sum_row: int               # the SUM-of-P&L row (for net-profit HLOOKUP)
    entity_first_col: int      # column of the first entity code in header_row


def parse_tb(path: str, codes) -> TBGeometry:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    hdr = _find_header_row(ws, codes) or C.REPORT_FIRST_DATA_ROW
    cols = [c.column for c in ws[hdr]
            if isinstance(c.value, str) and c.value.strip() in set(codes)]
    return TBGeometry(
        header_row=hdr,
        first_col=1,
        last_col=ws.max_column,
        last_data_row=ws.max_row,
        sum_row=ws.max_row,
        entity_first_col=min(cols) if cols else 1,
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
                     if isinstance(ws.cell(row=r, column=ac).value, (int, float))]
        blocks[label] = AggBlock(
            label=label, acct_col=ac, subheader_row=sub_row,
            first_data_row=min(data_rows) if data_rows else sub_row + 1,
            last_data_row=max(data_rows) if data_rows else ws.max_row,
            entity_cols=ents, last_entity_col=max(ents.values()),
        )
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


def parse_rates(path: str) -> RatesGeometry:
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    from_rc = _find_cell(ws, "From")
    rate_rc = _find_cell(ws, "Exch. Rate")
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
    code_col = {c.value.strip(): c.column for c in ws[hdr]
                if isinstance(c.value, str) and c.value.strip() in set(codes)}
    acct_rc = _find_cell(ws, "Group Account Number")
    acct_col = acct_rc[1] if acct_rc else 1
    accounts: dict[object, dict[str, float]] = {}
    for r in range(hdr + 1, ws.max_row + 1):
        acct = ws.cell(row=r, column=acct_col).value
        if not isinstance(acct, (int, float)):
            continue
        accounts[acct] = {
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
            acct = ws.cell(row=r, column=blk.acct_col).value
            if not isinstance(acct, (int, float)):
                continue
            table[acct] = {code: float(ws.cell(row=r, column=col).value or 0)
                           for code, col in blk.entity_cols.items()}
        out[label] = table
    return out
