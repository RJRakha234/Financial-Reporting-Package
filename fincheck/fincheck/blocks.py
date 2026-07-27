"""Reconstruct paragraphs and tables from a PDF, for side-by-side comparison.

Everything in :mod:`fincheck.compare` avoids this deliberately: a PDF holds
glyphs at coordinates, not paragraphs and tables, so any block structure is
inferred and can be wrong. Comparing two documents *as documents* needs that
structure anyway — you cannot put a paragraph beside its counterpart without
first deciding what a paragraph is — so this module makes the inference
explicitly, in one place, with its rules written down:

1. Spans sharing a text baseline form a **row**. Baselines are exact
   coordinates from the file, so this step is reliable.
2. A row is a **figure row** when its numbers form a run at the end of the
   line, after whatever labels it. That holds wherever on the page the column
   sits, so it needs no threshold: a clause number leads its text and stays
   prose, a figure quoted mid-sentence has words after it and stays prose, and
   a line of nothing but figures is a column header.
3. Consecutive figure rows form a **table**; consecutive prose rows form a
   **paragraph**.
4. A block continues across a page break when the text before the break does
   not end a sentence, or when a table resumes with the same column count.
   Statements set landscape in one document and portrait in another split at
   different points, so without this they would never line up.

Each rule can misfire. The blocks are a reading aid for a human reviewer, not
evidence — treat a difference as "look at this", and use the exact layers when
you need proof.
"""

import re
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from .columns import build_grid
from .numbers import parse_number

# Baselines within this many points are the same row.
_BASELINE_TOLERANCE = 2.0
# Tokens this short may follow a row's figures without making the line prose:
# footnote asterisks and daggers routinely do.
_TRAILING_MARKER = 2
# Labels longer than this are prose, however many numbers they contain.
_MAX_LABEL_WORDS = 16
# Past this many words, a line whose only figure is a bare year is a sentence
# that happens to end on a date, not a one-column table row.
_PROSE_WORDS = 4
# A baseline gap this many times the page's usual line pitch starts a new block.
_PARAGRAPH_GAP = 1.6
# Tables carry deliberate internal spacing around their sections, so they need a
# wider gap than prose before it means "a different table".
_TABLE_GAP = 3.2
# How far to look ahead for a figure row before deciding a label-only row ended
# the table rather than heading a section inside it.
_SECTION_LOOKAHEAD = 3
# How far above a line to look for the table row whose columns it shares.
_TABLE_LOOKBACK = 3
# Two figures this close to the same right edge are in the same column.
_COLUMN_MATCH = 2.0
# Sentence-final punctuation, used to decide whether a paragraph continues.
_ENDS_SENTENCE = re.compile(r"[.:;!?…]['\")\]]?\s*$")


@dataclass
class Figure:
    """One numeric cell of a reconstructed table row."""

    text: str
    value: float
    x0: float
    x1: float


@dataclass
class Row:
    page: int
    baseline: float
    label: str
    figures: list[Figure] = field(default_factory=list)
    # The label's words with their horizontal extent, so a table's column
    # headings can be read back off the page and put over the right columns.
    words: list[tuple[float, float, str]] = field(default_factory=list)
    # Extent of the row's words, so it can be tested against a marked region.
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    # Distance from the previous baseline on this page, and the page's usual
    # line pitch. Their ratio is what separates a new paragraph from a new line.
    gap: float = 0.0
    pitch: float = 0.0

    def breaks_after(self, kind: str) -> bool:
        """Is the gap before this row wide enough to end a block of ``kind``?"""
        if self.pitch <= 0:
            return False
        limit = _TABLE_GAP if kind == "table" else _PARAGRAPH_GAP
        return self.gap > limit * self.pitch

    @property
    def is_figure_row(self) -> bool:
        return bool(self.figures)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    def as_text(self) -> str:
        cells = "  ".join(f.text for f in self.figures)
        return f"{self.label}  {cells}".strip()


@dataclass
class Block:
    kind: str  # "paragraph" | "table"
    rows: list[Row]
    page_start: int
    page_end: int
    # The label-only rows that name this table's columns, in reading order. They
    # are not comparable content — they carry no figures and each document wraps
    # them differently — but they are what tells a reader which column a figure
    # is in, so they are kept beside the table rather than compared as rows.
    headers: list[Row] = field(default_factory=list)

    @property
    def text(self) -> str:
        if self.kind == "paragraph":
            return " ".join(r.label for r in self.rows if r.label).strip()
        return "\n".join(r.as_text() for r in self.rows)

    @property
    def labels(self) -> list[str]:
        return [r.label for r in self.rows if r.label]

    @property
    def label_text(self) -> str:
        return " ".join(self.labels)

    @property
    def figures(self) -> list[Figure]:
        return [f for r in self.rows for f in r.figures]

    @property
    def values(self) -> list[float]:
        return [f.value for f in self.figures]

    @property
    def n_columns(self) -> int:
        return max((len(r.figures) for r in self.rows), default=0)

    @property
    def pages(self) -> str:
        if self.page_start == self.page_end:
            return str(self.page_start)
        return f"{self.page_start}–{self.page_end}"

    @property
    def tokens(self) -> set:
        """Word set used to judge similarity against a block in the other file."""
        return _tokenise(self.label_text)


def _tokenise(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


_FOLIO_RE = re.compile(r"^(?:page\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?$", re.I)


def _is_folio(row: "Row") -> bool:
    """A page number, checked only against the page's first and last rows.

    A bare number with nothing labelling it now reads as a figure row, so the
    folio has to be recognised in that form too, not just as stray text.
    """
    if row.figures and not row.label:
        return len(row.figures) == 1 and 0 < row.figures[0].value <= 9999
    return not row.figures and bool(_FOLIO_RE.match(row.label.strip()))


def _is_number(word) -> bool:
    return parse_number(word[4].strip()) is not None


def _is_bare_year(text: str) -> bool:
    stripped = text.strip()
    return (
        len(stripped) == 4
        and stripped.isdigit()
        and 1900 <= int(stripped) <= 2099
    )


_FOOTNOTE_MARKER = re.compile(r"\(\s*(\d)\s*\)")


def _all_footnote_markers(figures: list[Figure]) -> bool:
    """Is every number on this line a footnote reference rather than a value?

    A footnote reference is a single parenthesised digit, and the references on
    one line are always distinct — "(1) (2) (3)" numbers three columns, it never
    states three amounts. Requiring distinctness is what keeps a genuine row of
    small bracketed losses, where a value can repeat, out of this rule; a real
    row of nothing but distinct single-digit negatives and no other figure is
    not something a statement contains.
    """
    if not figures:
        return False
    seen = set()
    for figure in figures:
        matched = _FOOTNOTE_MARKER.fullmatch(figure.text.strip())
        if matched is None or matched.group(1) in seen:
            return False
        seen.add(matched.group(1))
    return True


def _figures_trail_the_label(bucket, punctuation=()) -> bool:
    """Do this line's numbers form a run at its end, after any label text?

    Trailing markers of a character or two — a footnote asterisk, a dagger — are
    allowed to follow the figures, since they routinely do in a statement.
    ``punctuation`` holds the offsets of dashes that punctuate the label rather
    than state a nil cell; those are label, not figures.
    """
    started = False
    for offset, word in sorted(enumerate(bucket), key=lambda pair: pair[1][0]):
        if offset in punctuation:
            continue
        if _is_number(word):
            started = True
        elif started and len(word[4].strip()) > _TRAILING_MARKER:
            return False
    return started


def _rows_on_page(page: "fitz.Page", index: int) -> list[Row]:
    """Cluster the page's words into rows by vertical position.

    Words, not spans: a spreadsheet exported to PDF often writes a whole table
    row as one show-text operation, so testing spans for numberhood would file
    every figure on the row under its label. Word boxes come from the file
    either way, so nothing is being guessed here.
    """
    words = [w for w in page.get_text("words") if w[4].strip()]
    if not words:
        return []

    # (x0, y0, x1, y1, text, block, line, word_no)
    buckets: list[list] = []
    for word in sorted(words, key=lambda w: (w[3], w[0])):
        if buckets and abs(buckets[-1][0][3] - word[3]) <= _BASELINE_TOLERANCE:
            buckets[-1].append(word)
        else:
            buckets.append([word])

    # Pass one: split every row into words and figures, without yet deciding
    # whether those figures are table cells.
    candidates = []
    for bucket in buckets:
        bucket.sort(key=lambda w: w[0])
        text_parts, figures, placed = [], [], []
        punctuation = _dashes_in_the_label(bucket)
        for offset, word in enumerate(bucket):
            text = word[4].strip()
            value = None if offset in punctuation else parse_number(text)
            if value is None:
                text_parts.append(text)
                placed.append((round(word[0], 2), round(word[2], 2), text))
            else:
                figures.append(
                    Figure(
                        text=text,
                        value=value,
                        x0=round(word[0], 2),
                        x1=round(word[2], 2),
                    )
                )
        candidates.append(
            {
                "bucket": bucket,
                "label": re.sub(r"\s+", " ", " ".join(text_parts)).strip(),
                "figures": figures,
                "words": placed,
                "punctuation": punctuation,
                "tabular": False,
                # Rejected for a reason alignment cannot overturn: these are
                # footnote markers and dates, not amounts in a column.
                "settled": False,
            }
        )

    # Pass two: in a table row the figures form a run at the end of the line,
    # after whatever labels it. That one property separates every case that
    # matters, and needs no threshold on where the page the figures sit:
    #
    #   "Materialaufwand      456.789,12"   -> label, then figures: a row
    #   "1.1 'Services' means the ..."      -> figure leads the text: prose
    #   "...consideration of 1,234 crore"   -> text after the figure: prose
    #   "30   2,025   31   2,025"           -> figures only: a header row
    #
    # A long label is prose whatever its numbers do, which catches a sentence
    # that happens to end on a figure.
    for row in candidates:
        if row["figures"] and len(row["label"].split()) <= _MAX_LABEL_WORDS:
            row["tabular"] = _figures_trail_the_label(
                row["bucket"], row["punctuation"]
            )
            # A sentence ending on a year ("...PEAK Matrix Assessment 2025") is
            # still a sentence. Left alone it became a one-column table row, and
            # because the other document wrapped the same sentence differently it
            # then read as a figure that had disappeared.
            if (
                row["tabular"]
                and len(row["figures"]) == 1
                and _is_bare_year(row["figures"][0].text)
                and len(row["label"].split()) > _PROSE_WORDS
            ):
                row["tabular"], row["settled"] = False, True
            # "(1)" is a footnote marker, not a figure of -1. Read as figures,
            # the markers strung along a column heading — "Financial Services (1)
            # Retail (2) Communication (3)" — turn the heading band into data
            # rows that deviate against every counterpart, which is a false alarm
            # a reconciliation must never raise.
            if row["tabular"] and _all_footnote_markers(row["figures"]):
                row["tabular"], row["settled"] = False, True

    # Pass three: take back the rows the label-length rule rejected, where the
    # figures line up with a table's columns.
    _rescue_aligned_rows(candidates)

    rows: list[Row] = []
    for row in candidates:
        label, figures, words = row["label"], row["figures"], row["words"]
        if not row["tabular"]:
            # Keep the digits in the sentence they belong to.
            label = re.sub(
                r"\s+", " ", " ".join(w[4].strip() for w in row["bucket"])
            ).strip()
            figures = []
            words = [
                (round(w[0], 2), round(w[2], 2), w[4].strip())
                for w in row["bucket"]
            ]
        bucket = row["bucket"]
        rows.append(
            Row(
                page=index + 1,
                baseline=round(bucket[0][3], 2),
                label=label,
                figures=figures,
                words=words,
                x0=round(min(w[0] for w in bucket), 2),
                y0=round(min(w[1] for w in bucket), 2),
                x1=round(max(w[2] for w in bucket), 2),
                y1=round(max(w[3] for w in bucket), 2),
            )
        )

    # A bare number alone in the top or bottom margin is the folio, not content;
    # left in, it would be glued onto the first or last paragraph of the page.
    while rows and _is_folio(rows[0]):
        rows.pop(0)
    while rows and _is_folio(rows[-1]):
        rows.pop()

    # Line pitch as the median gap on the page: robust to the handful of large
    # gaps that separate blocks, which is exactly what we want to detect.
    gaps = [
        round(b.baseline - a.baseline, 2) for a, b in zip(rows, rows[1:])
    ]
    positive = sorted(g for g in gaps if g > 0)
    pitch = positive[len(positive) // 2] if positive else 0.0
    for row, gap in zip(rows[1:], gaps):
        row.gap = gap
        row.pitch = pitch
    return rows


def _continues(previous: Block, row: Row) -> bool:
    """Does ``row`` carry on the block interrupted by a page break?"""
    if previous.kind == "table":
        return row.is_figure_row and len(row.figures) == previous.n_columns
    return bool(previous.labels) and not _ENDS_SENTENCE.search(previous.labels[-1])


def _heads_a_section(rows: list[Row], i: int) -> bool:
    """Is ``rows[i]`` a label-only row heading a section inside a table?

    A statement reads "Financial assets" and then lists figures beneath it.
    Treating that heading as the end of the table would cut one balance sheet
    into a handful of fragments that no longer resemble the other document's.
    """
    row = rows[i]
    if row.is_figure_row or row.breaks_after("table"):
        return False
    for ahead in rows[i + 1 : i + 1 + _SECTION_LOOKAHEAD]:
        if ahead.is_figure_row:
            return not ahead.breaks_after("table")
        if ahead.breaks_after("table"):
            return False
    return False


# How many lines above a table can still be part of its column headings.
_MAX_HEADER_ROWS = 8
# Past this many words a line above a table is the sentence introducing it,
# unless it is set to the columns.
_HEADER_ROW_WORDS = 5
# A heading is aligned to its column, so it ends this close to the column's
# right edge. Looser than the figures' own tolerance: a heading set centred
# over a narrow column still lands near the edge, and the alternative is
# reading none of its names.
_HEADING_TOLERANCE = 9.0
# A word that names something: two letters together, not a figure, a note
# reference or a bracketed marker.
_NAMES_SOMETHING = re.compile(r"[A-Za-z]{2}")
# "(In ₹ crore)", "(Amounts in millions)" — the units note that sits between the
# introduction and the headings, and marks the top of the heading band.
_UNITS_NOTE = re.compile(
    r"\(?\s*(?:in|amounts?\s+in)\b.{0,24}?"
    r"(crores?|lakhs?|millions?|billions?|thousands?|units?)\b",
    re.I,
)


_BARE_DASH = re.compile(r"^[-–—]$")


def _dashes_in_the_label(bucket) -> set:
    """Which of this line's dashes punctuate it rather than state nil.

    A statement writes a nil cell as a dash, so a dash has to read as a figure.
    But it also writes "Liquid mutual fund units - carried at fair value through
    profit or loss", and read as a figure that dash puts a number in the middle
    of the line, which makes the whole row prose: its real amounts are then
    strung into a sentence instead of standing in their columns.

    A nil cell is in the figures at the end of the line. A dash with ordinary
    words still to come is punctuation.
    """
    found, seen_label = set(), False
    for offset in range(len(bucket) - 1, -1, -1):
        text = bucket[offset][4].strip()
        if _BARE_DASH.match(text):
            if seen_label:
                found.add(offset)
        elif parse_number(text) is None and len(text) > _TRAILING_MARKER:
            seen_label = True
    return found


def _rescue_aligned_rows(candidates: list[dict]) -> None:
    """Take back the table rows the label-length rule turned into prose.

    A statement whose middle column is a description — "Tax free bonds and
    government bonds - carried at amortized cost | Quoted price and market
    observable inputs | 1,408 | 1,812" — runs past any length a label can be
    given. Rejected for length, the whole table dissolves into a paragraph with
    its amounts strung through it, which is no way to check a figure against its
    counterpart.

    Alignment says what length cannot: prose does not put its numbers on the
    same right edge line after line. A line whose every figure sits in a column
    of a table row within a few lines of it is part of that table. Rescued rows
    become anchors themselves, so a table whose first rows are all long is
    recovered from whichever of its rows was short enough to be recognised.
    """
    while True:
        anchors = [
            (position, [f.x1 for f in row["figures"]])
            for position, row in enumerate(candidates)
            if row["tabular"]
        ]
        rescued = False
        for position, row in enumerate(candidates):
            if row["tabular"] or row["settled"] or not row["figures"]:
                continue
            if not _figures_trail_the_label(row["bucket"], row["punctuation"]):
                continue
            near = [
                edges
                for at, edges in anchors
                if 0 < abs(at - position) <= _TABLE_LOOKBACK
            ]
            if any(
                all(
                    any(abs(edge - f.x1) <= _COLUMN_MATCH for edge in edges)
                    for f in row["figures"]
                )
                for edges in near
            ):
                row["tabular"] = True
                rescued = True
        if not rescued:
            return


def _sits_over_columns(row: Row, edges: list[float]) -> bool:
    """Are this line's words set to the table's columns rather than run on?

    A heading is aligned to the column it names, so its words end where the
    figures end. Two such words is enough and is what separates a heading band
    from the sentence that introduces the table, which no length test can do:
    "Particulars Financial Services Manufacturing Retail Communication Total" is
    seven words, and so is a sentence.
    """
    # A column is named, never only numbered. Without this a row of figures the
    # numbering rules left as text — "(2,734) (149) (12) 2,998" — reads as a
    # heading band and its amounts are lost to the comparison. The test is on
    # the line, not the word: "June 30, 2025   March 31, 2025" names two columns
    # and the words that land on their edges are the years.
    if not _NAMES_SOMETHING.search(row.label):
        return False
    hits = {
        min(range(len(edges)), key=lambda i: abs(edges[i] - x1))
        for _, x1, _ in row.words
        if any(abs(edge - x1) <= _HEADING_TOLERANCE for edge in edges)
    }
    return len(hits) >= 2


def _header_band(rows: list[Row], start: int, edges: list[float]) -> list[Row]:
    """The lines above a table that name its columns.

    Walks back from the table's first row while the lines still look like
    headings: set to the columns, or short enough to be a heading that wrapped.
    The units note above the headings ends the walk, as does the sentence that
    introduces the table.
    """
    first = rows[start]
    band: list[Row] = []
    for row in reversed(rows[max(0, start - _MAX_HEADER_ROWS) : start]):
        if row.page != first.page or row.is_figure_row:
            break
        if _UNITS_NOTE.search(row.label):
            break
        if (
            not _sits_over_columns(row, edges)
            and len(row.label.split()) > _HEADER_ROW_WORDS
        ):
            break
        band.append(row)
        # A paragraph-sized gap above this line means the headings start here.
        if row.pitch > 0 and row.gap > _PARAGRAPH_GAP * row.pitch:
            break
    band.reverse()
    # Short lines alone are not a heading band — they are as likely the title of
    # the section. At least one line has to be set to the columns for the band
    # to be naming them; a heading that wrapped supplies the rest.
    if not any(_sits_over_columns(row, edges) for row in band):
        return []
    return band


def is_html(path: str) -> bool:
    """Is this an HTML filing rather than a PDF?"""
    return path.lower().endswith((".htm", ".html", ".xhtml"))


def segment(pdf_path: str) -> list[Block]:
    """Split a document into an ordered list of paragraph and table blocks.

    Reads a PDF by reconstructing structure from page geometry, or an HTML
    filing by reading the structure it states. The blocks are the same either
    way, so nothing downstream needs to know which it got.
    """
    if is_html(pdf_path):
        from .htmlblocks import segment_html

        return segment_html(pdf_path)

    doc = fitz.open(pdf_path)
    try:
        rows: list[Row] = []
        for index, page in enumerate(doc):
            rows.extend(
                r for r in _rows_on_page(page, index) if r.label or r.figures
            )
    finally:
        doc.close()

    blocks: list[Block] = []
    starts: list[int] = []
    for i, row in enumerate(rows):
        current = blocks[-1] if blocks else None
        in_table = current is not None and current.kind == "table"

        if row.is_figure_row:
            kind = "table"
        elif in_table and _heads_a_section(rows, i):
            kind = "table"
        else:
            kind = "paragraph"

        if current is not None and current.kind == kind:
            if current.page_end == row.page:
                joins = not row.breaks_after(kind)
            else:
                joins = _continues(current, row)
            if joins:
                current.rows.append(row)
                current.page_end = row.page
                continue

        blocks.append(
            Block(kind=kind, rows=[row], page_start=row.page, page_end=row.page)
        )
        starts.append(i)

    for block, start in zip(blocks, starts):
        if block.kind != "table":
            continue
        grid = build_grid(block.rows)
        if len(grid):
            block.headers = _header_band(rows, start, grid.edges)
    return blocks
