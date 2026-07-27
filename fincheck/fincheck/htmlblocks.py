"""Read an HTML filing into the same blocks a PDF is reconstructed into.

Everything hard about reading a PDF is an inference. A PDF says *draw the
string "11,796" at (412, 508)*; that it belongs to a table, that the table has
nine columns, and that this figure sits in the sixth of them are all conclusions
:mod:`fincheck.blocks` reaches from geometry, and each of them can be wrong.

HTML states them. ``<tr>`` *is* the row and ``<td>`` *is* the cell, so reading
an HTML exhibit removes the largest source of error in the whole tool rather
than mitigating it. Three failure modes disappear outright:

* **Clipping.** A printed page cuts at the margin; the markup holds the whole
  table however wide it is. A real filing lost three of its eight business
  segments this way when it was converted to PDF for review — the tool
  correctly reported figures absent from the PDF that were never absent from
  the filing.
* **Encoding.** ``&#8377;`` is unambiguous. The same sign came out of a
  PDF conversion as the letters ``ru``, which hid 43 figures.
* **Column drift.** Cells are compared by position in the row. Read from
  markup that position is stated; read from geometry it is inferred from x
  coordinates, and one inserted column misaligns everything after it.

Only the standard library is used, so this adds no dependency. EDGAR wrappers
(``<DOCUMENT><TYPE>…<TEXT>``) are tolerated, as are the uppercase tags and
unclosed ``<td>`` elements that real filings are full of.

**HTML has no pages.** Rows carry a synthetic ordinate that preserves document
order — which is all the alignment needs — and report page (1). Nothing is
invented about where a passage would fall on paper.
"""

import html as html_module
import re
from html.parser import HTMLParser

from .blocks import Block, Figure, Row
from .numbers import is_numberish, parse_number

# Cells are laid out on this pitch so a figure's synthetic x reflects the
# column it is actually in. Nothing measures these; they only have to order.
_COL_WIDTH = 60.0
_LABEL_X = 60.0
_LINE = 14.0

# Tags whose content is not document text.
_SKIP = {"script", "style", "head", "title"}
# A block-level tag ends the run of text being collected.
_BREAK = {
    "p", "div", "br", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "ul", "ol", "hr",
}


class _Reader(HTMLParser):
    """Collect tables as tables and everything else as prose, in order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list = []  # ("prose", text) | ("table", [[cell, ...], ...])
        self._text: list[str] = []
        self._tables: list[list] = []   # stack, for tables nested in tables
        self._cell: list[str] | None = None
        self._skip = 0
        # Footnote markers set as superscripts sit *inside* the cell that
        # carries the figure — "17,710<SUP>(2)</SUP>". Read as part of the
        # value the cell stops being a number at all, and the figure vanishes
        # from the comparison entirely. The PDF path already tolerates a
        # trailing marker; this is the same tolerance, stated in markup.
        self._sup = 0

    # -- text ------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        if self._skip or (self._sup and self._cell is not None):
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._text.append(data)

    def _flush_prose(self) -> None:
        text = _clean(" ".join(self._text))
        self._text = []
        if text:
            self.items.append(("prose", text))

    # -- tags ------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "sup":
            self._sup += 1
            return
        if tag == "table":
            self._flush_prose()
            self._close_cell()
            self._tables.append([])
            return
        if tag == "tr" and self._tables:
            self._close_cell()
            self._tables[-1].append([])
            return
        if tag in ("td", "th") and self._tables:
            self._close_cell()
            if not self._tables[-1]:
                self._tables[-1].append([])
            self._cell = []
            return
        if tag in _BREAK:
            if self._cell is not None:
                self._cell.append(" ")
            else:
                self._text.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "sup":
            self._sup = max(0, self._sup - 1)
            return
        if tag in ("td", "th"):
            self._close_cell()
            return
        if tag == "tr":
            self._close_cell()
            return
        if tag == "table" and self._tables:
            self._close_cell()
            rows = [r for r in self._tables.pop() if any(c.strip() for c in r)]
            if rows:
                # A table nested for layout contributes its rows to the table
                # that contains it, rather than starting a second one.
                if self._tables:
                    self._tables[-1].extend(rows)
                else:
                    self.items.append(("table", rows))
            return
        if tag in _BREAK:
            if self._cell is not None:
                self._cell.append(" ")
            else:
                self._text.append(" ")

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        text = _clean("".join(self._cell))
        self._cell = None
        self._sup = 0
        if self._tables and self._tables[-1]:
            self._tables[-1][-1].append(text)

    def close(self) -> None:  # noqa: D102
        super().close()
        # Unclosed tables are normal in filings; keep whatever they held.
        while self._tables:
            self._close_cell()
            rows = [r for r in self._tables.pop() if any(c.strip() for c in r)]
            if rows:
                self.items.append(("table", rows))
        self._flush_prose()


_WS = re.compile(r"[\s  ]+")


# A cell holding only a dash is an explicit nil, as it is in a printed
# statement. Written out, it must read as zero and not as a label.
_NIL = {"-", "--", "\u2013", "\u2014", "\u2212", "nil", "n/a", "na"}


def _is_nil(text: str) -> bool:
    return text.strip().lower() in _NIL


def _clean(text: str) -> str:
    return _WS.sub(" ", html_module.unescape(text)).strip()


def _strip_edgar(source: str) -> str:
    """Drop the EDGAR ``<DOCUMENT>`` envelope if one is present."""
    start = source.upper().find("<TEXT>")
    return source[start + 6 :] if start != -1 else source


# A run of prose this long is a paragraph in its own right rather than a stray
# fragment between tags.
_MIN_PROSE = 2


def _row_from_cells(cells: list[str], ordinate: float) -> Row | None:
    """One ``<tr>`` as a row: leading text is the label, numeric cells figures.

    The cell's position in the row is its column, which is the whole point of
    reading markup — no coordinate has to be interpreted to know it.
    """
    label_parts: list[str] = []
    figures: list[Figure] = []
    # A blank between two figures is a column with nothing in it — a nil. The
    # same statement exported to PDF writes a dash there, which reads as nil,
    # so the two must agree or every nil row differs by construction. Blanks
    # before the first figure are layout, and are left alone.
    pending: list[int] = []
    for index, cell in enumerate(cells):
        text = cell.strip()
        if not text:
            if figures:
                pending.append(index)
            continue
        value = parse_number(text) if is_numberish(text) else None
        if value is None and _is_nil(text):
            value = 0.0
        if value is not None:
            for blank in pending:
                x0 = _LABEL_X + (blank + 1) * _COL_WIDTH
                figures.append(
                    Figure(text="–", value=0.0, x0=x0, x1=x0 + _COL_WIDTH)
                )
            pending = []
            x0 = _LABEL_X + (index + 1) * _COL_WIDTH
            figures.append(
                Figure(text=text, value=value, x0=x0, x1=x0 + _COL_WIDTH)
            )
            continue
        # Not a figure: part of what labels the row. Anything pending was
        # trailing layout after all, not a column.
        pending = []
        label_parts.append(text)

    label = " ".join(label_parts).strip()
    if not label and not figures:
        return None
    width = _LABEL_X + (len(cells) + 1) * _COL_WIDTH
    return Row(
        page=1,
        baseline=ordinate,
        label=label,
        figures=figures,
        x0=_LABEL_X,
        y0=ordinate,
        x1=width,
        y1=ordinate + _LINE,
    )


def _prose_rows(text: str, ordinate: float) -> list[Row]:
    return [
        Row(
            page=1,
            baseline=ordinate,
            label=text,
            figures=[],
            x0=_LABEL_X,
            y0=ordinate,
            x1=_LABEL_X + len(text) * 4.0,
            y1=ordinate + _LINE,
        )
    ]


def segment_html(path: str) -> list[Block]:
    """Split an HTML filing into paragraph and table blocks.

    The counterpart of :func:`fincheck.blocks.segment`, and deliberately the
    same shape: everything downstream — matching, scoring, the word diff, the
    report — is unchanged by which of the two produced its input.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        source = _strip_edgar(fh.read())

    reader = _Reader()
    reader.feed(source)
    reader.close()

    blocks: list[Block] = []
    ordinate = 0.0
    for kind, payload in reader.items:
        if kind == "prose":
            if len(payload.split()) < _MIN_PROSE:
                continue
            ordinate += _LINE
            blocks.append(
                Block(
                    kind="paragraph",
                    rows=_prose_rows(payload, ordinate),
                    page_start=1,
                    page_end=1,
                )
            )
            continue

        rows: list[Row] = []
        for cells in payload:
            ordinate += _LINE
            row = _row_from_cells(cells, ordinate)
            if row is not None:
                rows.append(row)
        if not rows:
            continue
        # A table carrying no figures anywhere is a layout device — a header
        # band, a signature block — and reads as prose, exactly as the PDF
        # reconstruction treats one.
        if any(r.figures for r in rows):
            blocks.append(
                Block(kind="table", rows=rows, page_start=1, page_end=1)
            )
        else:
            text = " ".join(r.label for r in rows if r.label).strip()
            if text:
                blocks.append(
                    Block(
                        kind="paragraph",
                        rows=_prose_rows(text, rows[0].baseline),
                        page_start=1,
                        page_end=1,
                    )
                )
    return blocks


def figures_in_html(path: str) -> list:
    """Every figure printed in an HTML filing, for the ledger.

    Returns ``fincheck.ledger.Occurrence`` records, so the reconciliation is
    the same reconciliation whichever format each side arrives in.
    """
    from .ledger import Occurrence

    with open(path, encoding="utf-8", errors="replace") as fh:
        source = _strip_edgar(fh.read())
    reader = _Reader()
    reader.feed(source)
    reader.close()

    found: list = []
    for kind, payload in reader.items:
        if kind == "prose":
            context = payload[:120]
            for token in payload.split():
                value = parse_number(token) if is_numberish(token) else None
                if value is not None:
                    found.append(
                        Occurrence(value=value, page=1, context=context)
                    )
            continue
        for cells in payload:
            context = " ".join(c for c in cells if c.strip())[:120]
            for cell in cells:
                text = cell.strip()
                if not text:
                    continue
                if is_numberish(text):
                    value = parse_number(text)
                    if value is not None:
                        found.append(
                            Occurrence(value=value, page=1, context=context)
                        )
                        continue
                # A figure quoted inside a sentence in a cell still counts.
                for token in text.split():
                    value = parse_number(token) if is_numberish(token) else None
                    if value is not None:
                        found.append(
                            Occurrence(value=value, page=1, context=context)
                        )
    return found
