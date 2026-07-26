"""Pair up two documents' content and diff each pair.

Side-by-side comparison cannot use page positions: one document sets a
statement landscape over two pages, the other portrait over three, so page 4 of
one has little to do with page 4 of the other. Content is matched by what it
says instead.

**The unit of comparison differs by kind, and it has to.** Prose is compared a
paragraph at a time, because the two documents wrap their lines at different
measures and no line of one corresponds to a line of the other. Tables are
compared a row at a time, because the two documents group their rows into
tables differently — one keeps a balance sheet whole, the other splits it over
three pages — so whole-table matching collapses on exactly the statements that
matter most.

Matching runs in two stages, the way a good text diff does:

1. **Anchors.** Any unit whose content occurs exactly once in each document is
   an unambiguous pairing. The longest run of these that preserves order fixes
   the shape of the alignment cheaply.
2. **Fuzzy fill.** Between consecutive anchors, the leftovers get a full
   order-preserving alignment on similarity. Order preservation matters:
   financial statements repeat labels ("Total", "Others", "Investments")
   endlessly, and a matcher free to reorder pairs the wrong ones.

Similarity is deliberately crude and explainable — shared words for prose,
shared words and shared figures for table rows — so a reviewer can see why two
things were put beside each other.
"""

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field

from .blocks import Block, Row

# Below these, two units are unrelated and better reported as one-sided.
PARAGRAPH_FLOOR = 0.45
ROW_FLOOR = 0.34
# A fuzzy-fill segment larger than this is left unmatched rather than aligned;
# beyond it the quadratic fill costs more than the pairing is worth.
_MAX_SEGMENT = 320
# Inside a section the reviewer has numbered in both documents, correspondence
# is asserted rather than inferred, so pairing needs far less similarity.
_MARKED_FLOOR_SCALE = 0.35
# A one-sided run longer than this is content of its own, not an insertion into
# the surrounding block.
_MAX_INSERTION = 8
# A standalone paragraph up to this long may be treated as a heading.
_MAX_HEADING = 120
# What a heading looks like in a financial statement: a numbered note, a
# statement title, or a line set in capitals.
_HEADING_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*[.)]?\s+\S"
    r"|(?:Condensed|Consolidated|Interim|Statement of|Balance Sheet|Notes? to)\b"
    r"|[A-Z][A-Z0-9 ,\-&/()'’]{5,}$)"
)
# A paragraph occurring more often than this is a running page header.
_RUNNING_HEADER = 3
# A statement title, found anywhere in a line. The running header is often glued
# to it ("INFOSYS LIMITED AND SUBSIDIARIES Condensed Consolidated Balance
# Sheet"), so the title has to be pulled out rather than matched from the start.
_STATEMENT_RE = re.compile(
    r"(?:Condensed\s+)?(?:Consolidated\s+)?(?:Interim\s+)?"
    r"(?:Statement of [A-Za-z][A-Za-z ]{2,60}|Balance Sheets?)",
    re.I,
)


def _heading_of(text: str) -> str:
    """The heading this paragraph provides for what follows, or ``""``."""
    found = _STATEMENT_RE.search(text)
    if found:
        return found.group(0).strip()
    if len(text) <= _MAX_HEADING and _HEADING_RE.match(text):
        return text
    return ""


@dataclass
class Unit:
    """One comparable thing: a whole paragraph, or a single table row."""

    kind: str  # "paragraph" | "row"
    block_index: int
    block: Block
    row: Row | None
    text: str
    tokens: set
    values: tuple
    section: str | None = None
    # The rows this unit was built from, so its place on the page can be found
    # again when marking up the source PDF.
    rows: tuple = ()

    @property
    def page(self) -> str:
        return self.block.pages if self.row is None else str(self.row.page)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _runs_by_section(rows, marks):
    """Group consecutive rows by the marked section they fall in.

    With no marks this yields one run, so an unmarked document keeps whole
    paragraphs exactly as before.
    """
    if marks is None:
        return [(None, list(rows))]
    runs: list[tuple] = []
    for row in rows:
        label = marks.label_for(row.page, row.bbox)
        if runs and runs[-1][0] == label:
            runs[-1][1].append(row)
        else:
            runs.append((label, [row]))
    return runs


def units_of(blocks: list[Block], marks=None) -> list[Unit]:
    """Flatten blocks into comparable units, tagged with any marked section.

    A paragraph takes the section its first marked row falls in, so a paragraph
    straddling a mark boundary still lands in one section rather than none.
    """

    def section_of(rows) -> str | None:
        if marks is None:
            return None
        for row in rows:
            label = marks.label_for(row.page, row.bbox)
            if label is not None:
                return label
        return None

    out: list[Unit] = []
    for index, block in enumerate(blocks):
        if block.kind == "paragraph":
            # Split the paragraph wherever the marked section changes. A block
            # reconstructed from geometry can run straight through two or three
            # of a reviewer's marks; left whole it would take the first one's
            # number and silently swallow the rest, which is how sections went
            # missing entirely.
            for label, rows in _runs_by_section(block.rows, marks):
                text = " ".join(r.label for r in rows if r.label).strip()
                if not text:
                    continue
                out.append(
                    Unit(
                        kind="paragraph",
                        block_index=index,
                        block=block,
                        row=rows[0] if label is not None else None,
                        text=text,
                        tokens=set(_norm(text).split()),
                        values=(),
                        section=label,
                        rows=tuple(rows),
                    )
                )
        else:
            for row in block.rows:
                out.append(
                    Unit(
                        kind="row",
                        block_index=index,
                        block=block,
                        row=row,
                        text=row.label,
                        tokens=set(_norm(row.label).split()),
                        values=tuple(f.value for f in row.figures),
                        section=section_of([row]),
                        rows=(row,),
                    )
                )
    return out


# --------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------


@dataclass
class Pair:
    a: Unit | None
    b: Unit | None
    similarity: float = 0.0
    words: list[tuple[str, str]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return (self.a or self.b).kind

    @property
    def status(self) -> str:
        if self.a is None:
            return "added"
        if self.b is None:
            return "removed"
        if self.kind == "row":
            if len(self.a.values) != len(self.b.values):
                return "columns-differ"
            if self.a.values != self.b.values:
                return "figures-differ"
            return "same" if _norm(self.a.text) == _norm(self.b.text) else "label-differs"
        return "same" if all(op == "=" for op, _ in self.words) else "changed"

    @property
    def changed(self) -> bool:
        return self.status != "same"

    @property
    def changed_figures(self) -> list[tuple[int, float | None, float | None]]:
        """``(column, A value, B value)`` for every column that differs."""
        if self.a is None or self.b is None or self.kind != "row":
            return []
        va, vb = self.a.values, self.b.values
        out = []
        for i in range(max(len(va), len(vb))):
            x = va[i] if i < len(va) else None
            y = vb[i] if i < len(vb) else None
            if x != y:
                out.append((i, x, y))
        return out


def _jaccard(x: set, y: set) -> float:
    if not x and not y:
        return 1.0
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def similarity(a: Unit, b: Unit) -> float:
    if a.kind != b.kind:
        return 0.0
    label = _jaccard(a.tokens, b.tokens)
    if a.kind == "paragraph":
        return label
    if not a.values and not b.values:
        return label
    figures = _jaccard(set(a.values), set(b.values))
    # An unlabelled row — a nil line, a continuation — is identified only by its
    # figures, so a missing label must not veto the pairing.
    if not a.tokens and not b.tokens:
        return figures
    return 0.6 * label + 0.4 * figures


def _scored(a: Unit, b: Unit, floor_scale: float = 1.0) -> float:
    s = similarity(a, b)
    floor = (PARAGRAPH_FLOOR if a.kind == "paragraph" else ROW_FLOOR) * floor_scale
    return s if s >= floor else 0.0


def _align_window(
    a: list[Unit], b: list[Unit], floor_scale: float = 1.0
) -> list[Pair]:
    """Order-preserving alignment maximising total similarity; gaps are free.

    Free gaps suit documents that genuinely differ in extent — one carries an
    auditor's report the other does not — where charging for the unmatched
    material would drag real pairs out of alignment.
    """
    rows, cols = len(a), len(b)
    if not rows or not cols:
        return [Pair(a=x, b=None) for x in a] + [Pair(a=None, b=y) for y in b]
    if rows * cols > _MAX_SEGMENT * _MAX_SEGMENT:
        return [Pair(a=x, b=None) for x in a] + [Pair(a=None, b=y) for y in b]

    scores = [
        [_scored(a[i], b[j], floor_scale) for j in range(cols)] for i in range(rows)
    ]
    table = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        row_i, row_next, srow = table[i], table[i + 1], scores[i]
        for j in range(cols - 1, -1, -1):
            best = row_next[j] if row_next[j] >= row_i[j + 1] else row_i[j + 1]
            s = srow[j]
            if s > 0:
                paired = row_next[j + 1] + s
                if paired > best:
                    best = paired
            row_i[j] = best

    pairs: list[Pair] = []
    i = j = 0
    while i < rows and j < cols:
        s = scores[i][j]
        if s > 0 and table[i][j] == table[i + 1][j + 1] + s:
            pairs.append(Pair(a=a[i], b=b[j], similarity=round(s, 3)))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            pairs.append(Pair(a=a[i], b=None))
            i += 1
        else:
            pairs.append(Pair(a=None, b=b[j]))
            j += 1
    pairs += [Pair(a=x, b=None) for x in a[i:]]
    pairs += [Pair(a=None, b=y) for y in b[j:]]
    return pairs


def _key(unit: Unit):
    return (unit.kind, _norm(unit.text), unit.values)


def _anchor_pairs(a: list[Unit], b: list[Unit]) -> list[tuple[int, int]]:
    """Indices of units whose content occurs exactly once on each side.

    Kept in increasing order on both sides via a longest-increasing-subsequence
    pass, so the anchors can only ever describe a consistent alignment.
    """
    keys_a = [_key(u) for u in a]
    keys_b = [_key(u) for u in b]
    count_a, count_b = Counter(keys_a), Counter(keys_b)
    where_b = {k: i for i, k in enumerate(keys_b) if count_b[k] == 1}

    candidates = [
        (i, where_b[k])
        for i, k in enumerate(keys_a)
        if count_a[k] == 1 and k in where_b and (a[i].tokens or a[i].values)
    ]
    if not candidates:
        return []

    # Longest strictly increasing subsequence on the B index.
    import bisect

    tails: list[int] = []
    back: list[int] = []
    parent = [-1] * len(candidates)
    for n, (_, jb) in enumerate(candidates):
        pos = bisect.bisect_left(tails, jb)
        if pos == len(tails):
            tails.append(jb)
            back.append(n)
        else:
            tails[pos] = jb
            back[pos] = n
        parent[n] = back[pos - 1] if pos else -1

    chain: list[int] = []
    node = back[-1] if back else -1
    while node != -1:
        chain.append(node)
        node = parent[node]
    return [candidates[n] for n in reversed(chain)]


def align(
    units_a: list[Unit],
    units_b: list[Unit],
    floor_scale: float = 1.0,
    pair_leftovers: bool = False,
) -> list[Pair]:
    """Pair the two documents' units and diff every matched pair."""
    anchors = _anchor_pairs(units_a, units_b)

    pairs: list[Pair] = []
    prev_a = prev_b = 0
    for ia, ib in anchors + [(len(units_a), len(units_b))]:
        pairs += _align_window(units_a[prev_a:ia], units_b[prev_b:ib], floor_scale)
        if ia < len(units_a) and ib < len(units_b):
            pairs.append(Pair(a=units_a[ia], b=units_b[ib], similarity=1.0))
        prev_a, prev_b = ia + 1, ib + 1

    if pair_leftovers:
        pairs = _fuse_one_sided(pairs)

    for pair in pairs:
        if pair.kind == "paragraph" and pair.a is not None and pair.b is not None:
            pair.words = diff_words(pair.a.text, pair.b.text)
    return pairs


def _fuse_one_sided(pairs: list[Pair]) -> list[Pair]:
    """Turn leftover blanks into comparisons, in order.

    Used only inside a section a reviewer has numbered in both documents. There,
    "present in one document only" is a statement the reviewer has already
    contradicted, so anything still unpaired is put beside its opposite number
    rather than shown against a blank. Order decides which goes with which,
    since within one marked section that is the only information left.
    """
    spare_b = [i for i, p in enumerate(pairs) if p.a is None]
    taken: set = set()
    for index, pair in enumerate(pairs):
        if pair.b is not None:
            continue
        for j in spare_b:
            if j in taken or pairs[j].b.kind != pair.a.kind:
                continue
            pairs[index] = Pair(a=pair.a, b=pairs[j].b, similarity=0.0)
            taken.add(j)
            break
    return [p for i, p in enumerate(pairs) if i not in taken]


def _full_text(unit: Unit) -> str:
    """A unit's text including any figures, for merging into a passage."""
    if unit.row is not None and unit.kind == "row":
        return unit.row.as_text()
    return unit.text


def _merge_paragraphs(units: list[Unit]) -> Unit:
    """Fuse a section's prose into one comparable passage."""
    text = " ".join(_full_text(u) for u in units if _full_text(u)).strip()
    first = units[0]
    return Unit(
        kind="paragraph",
        block_index=first.block_index,
        block=first.block,
        row=first.row,
        text=text,
        tokens=set(_norm(text).split()),
        values=(),
        section=first.section,
        rows=tuple(r for u in units for r in u.rows),
    )


def _align_marked_section(sub_a: list[Unit], sub_b: list[Unit]) -> list[Pair]:
    """Compare one section a reviewer numbered in both documents.

    Prose is fused into a single passage per side. The two documents rarely break
    a section into the same number of paragraphs — one may run it together where
    the other splits it in three — and pairing those counts against each other is
    what leaves passages facing a blank. Since the reviewer has already said the
    two regions correspond, the whole of one is compared against the whole of the
    other and the word-level diff locates the differences inside it.

    Table rows are kept individually, because a row means something on its own
    and a figure has to be comparable to its counterpart.
    """
    rows_a = [u for u in sub_a if u.kind == "row"]
    rows_b = [u for u in sub_b if u.kind == "row"]
    prose_a = [u for u in sub_a if u.kind == "paragraph"]
    prose_b = [u for u in sub_b if u.kind == "paragraph"]

    out: list[Pair] = []
    if rows_a and rows_b:
        aligned = align(
            rows_a, rows_b, floor_scale=_MARKED_FLOOR_SCALE, pair_leftovers=True
        )
        out += [p for p in aligned if p.a is not None and p.b is not None]
        # A line one document read as a table row and the other read as prose
        # leaves a row with no row to face. Rather than show it against a blank,
        # hand it to the prose comparison, where its words can still be diffed.
        prose_a = prose_a + [p.a for p in aligned if p.b is None]
        prose_b = prose_b + [p.b for p in aligned if p.a is None]
    else:
        prose_a, prose_b = prose_a + rows_a, prose_b + rows_b

    if prose_a and prose_b:
        merged = Pair(
            a=_merge_paragraphs(prose_a), b=_merge_paragraphs(prose_b), similarity=1.0
        )
        merged.words = diff_words(merged.a.text, merged.b.text)
        out.append(merged)
    else:
        out += [Pair(a=u, b=None) for u in prose_a]
        out += [Pair(a=None, b=u) for u in prose_b]
    return out


# A heading up to this long starts a section of its own.
_MAX_SECTION_HEADING = 90
# Two derived headings sharing this much of their wording are the same section.
_HEADING_FLOOR = 0.4


def _is_section_heading(text: str) -> bool:
    """Does this passage introduce a section rather than belong to one?

    Learned from how reviewers actually mark these documents up: they cut at
    the headings — "Client wins & Testimonials", "Recognitions & Awards",
    "About Infosys", "Extracted from the Condensed Consolidated Balance
    Sheet" — and let everything under one run together.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _MAX_SECTION_HEADING:
        return False
    # A heading is not a sentence. Checked first, because a numbered list item
    # ("1. Infosys announced ...") otherwise matches the numbered-note pattern
    # and starts a section per bullet.
    if stripped.endswith("."):
        return False
    if _STATEMENT_RE.search(stripped) or _HEADING_RE.match(stripped):
        return True
    # A short line set mostly in initial capitals.
    if len(stripped) > 60:
        return False
    words = stripped.split()
    if not 1 <= len(words) <= 8:
        return False
    capitalised = sum(1 for w in words if w[:1].isupper())
    return capitalised >= max(1, len(words) // 2)


def _heading_key(text: str) -> str:
    """A heading reduced to its words, so punctuation and bullets do not divide.

    One document writes "Key highlights :" and the other "Key highlights:"; one
    prefixes its bullets with a middle dot and the other with a bullet. None of
    that changes which section it is.
    """
    return " ".join(re.findall(r"[a-z0-9&]+", text.lower()))[:_MAX_SECTION_HEADING]


def derive_sections(units: list[Unit]) -> int:
    """Cut an unmarked document into sections at its headings.

    Sections are keyed by the heading's own words rather than by position, so
    the two documents pair on what a section *is*. One of them carrying an
    extra heading then shifts nothing else out of alignment.

    Returns the number of sections found. Units before the first heading get no
    section and fall back to ordinary content matching.
    """
    current = None
    found = 0
    for unit in units:
        # Test the unit's first line, not the whole of it. A heading is often
        # reconstructed together with the body beneath it — "About Infosys
        # Infosys is a global leader in ..." — and testing the merged text
        # would find no headings at all.
        lead = unit.rows[0].label.strip() if unit.rows else unit.text
        if unit.kind == "paragraph" and _is_section_heading(lead):
            current = _heading_key(lead)
            found += 1
        unit.section = current
    return found


def _runs(units: list[Unit]) -> list[tuple]:
    """Consecutive units sharing a section key, in document order."""
    out: list[tuple] = []
    for unit in units:
        if out and out[-1][0] == unit.section:
            out[-1][1].append(unit)
        else:
            out.append((unit.section, [unit]))
    return out


def _align_keys(keys_a: list, keys_b: list) -> list[tuple]:
    """Order-preserving alignment of two heading sequences on word overlap.

    Derived headings are not identical across two documents and one document
    may carry an extra one, so pairing them by name alone strands whole
    sections. Aligning the sequences tolerates both.
    """
    rows, cols = len(keys_a), len(keys_b)
    sets_a = [set(k.split()) if k else set() for k in keys_a]
    sets_b = [set(k.split()) if k else set() for k in keys_b]

    def score(i, j):
        if keys_a[i] is None or keys_b[j] is None:
            return 0.0
        s = _jaccard(sets_a[i], sets_b[j])
        return s if s >= _HEADING_FLOOR else 0.0

    table = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            best = max(table[i + 1][j], table[i][j + 1])
            s = score(i, j)
            if s > 0:
                best = max(best, table[i + 1][j + 1] + s)
            table[i][j] = best

    out: list[tuple] = []
    i = j = 0
    while i < rows and j < cols:
        s = score(i, j)
        if s > 0 and table[i][j] == table[i + 1][j + 1] + s:
            out.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            out.append((i, None))
            i += 1
        else:
            out.append((None, j))
            j += 1
    out += [(k, None) for k in range(i, rows)]
    out += [(None, k) for k in range(j, cols)]
    return out


def align_by_derived_sections(units_a: list[Unit], units_b: list[Unit]) -> list[Pair]:
    """Compare two documents cut at their own headings."""
    runs_a, runs_b = _runs(units_a), _runs(units_b)
    keys_a = [k for k, _ in runs_a]
    keys_b = [k for k, _ in runs_b]

    pairs: list[Pair] = []
    for i, j in _align_keys(keys_a, keys_b):
        sub_a = runs_a[i][1] if i is not None else []
        sub_b = runs_b[j][1] if j is not None else []
        if sub_a and sub_b:
            # Give the pair a shared name so the report groups it as one
            # section and labels it with the heading it was cut at.
            label = keys_a[i] or keys_b[j]
            for unit in sub_a + sub_b:
                unit.section = label
            pairs += _align_marked_section(sub_a, sub_b)
        else:
            pairs += align(sub_a, sub_b)
    return pairs


def align_by_section(units_a: list[Unit], units_b: list[Unit]) -> list[Pair]:
    """Align within each numbered section, then across the unmarked remainder.

    A section numbered in both documents is aligned only against itself, so its
    content cannot drift into a neighbouring section and cannot be reported as
    one-sided. Unmarked content falls back to ordinary content matching.
    """
    labels_a = [u.section for u in units_a]
    labels_b = [u.section for u in units_b]

    # Section order follows document A, then any section only B numbers.
    ordered: list[str] = []
    for label in labels_a + labels_b:
        if label is not None and label not in ordered:
            ordered.append(label)

    pairs: list[Pair] = []
    for label in ordered:
        sub_a = [u for u in units_a if u.section == label]
        sub_b = [u for u in units_b if u.section == label]
        if sub_a and sub_b:
            pairs += _align_marked_section(sub_a, sub_b)
        else:
            pairs += align(sub_a, sub_b)

    unmarked_a = [u for u in units_a if u.section is None]
    unmarked_b = [u for u in units_b if u.section is None]
    if unmarked_a or unmarked_b:
        pairs += align(unmarked_a, unmarked_b)
    return pairs


def diff_words(a: str, b: str) -> list[tuple[str, str]]:
    """Word-level diff of two paragraphs, as ``(op, text)`` runs."""
    wa, wb = a.split(), b.split()
    out: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, [w.lower() for w in wa], [w.lower() for w in wb], autojunk=False
    ).get_opcodes():
        if op == "equal":
            out.append(("=", " ".join(wb[j1:j2])))
        elif op == "delete":
            out.append(("-", " ".join(wa[i1:i2])))
        elif op == "insert":
            out.append(("+", " ".join(wb[j1:j2])))
        else:
            out.append(("-", " ".join(wa[i1:i2])))
            out.append(("+", " ".join(wb[j1:j2])))
    return out


# --------------------------------------------------------------------------
# Grouping back into displayable sections
# --------------------------------------------------------------------------


@dataclass
class Section:
    """A run of pairs shown together: one table, or one paragraph."""

    kind: str
    a_block: Block | None
    b_block: Block | None
    pairs: list[Pair] = field(default_factory=list)
    # The number a reviewer marked on this content, if any.
    marked: str | None = None
    # Position in the report, stamped onto the marked-up PDFs so a point here
    # can be found on the page it came from.
    serial: int = 0

    @property
    def changed(self) -> int:
        return sum(1 for p in self.pairs if p.changed)

    @property
    def status(self) -> str:
        if all(p.a is None for p in self.pairs):
            return "added"
        if all(p.b is None for p in self.pairs):
            return "removed"
        return "changed" if self.changed else "same"

    # Nearest preceding short paragraph, which in a financial statement is the
    # heading the table belongs under. A wide table's own first rows are wrapped
    # column headings ("reserve", "Company"), which make hopeless titles.
    heading: str = ""

    @property
    def title(self) -> str:
        if self.marked is not None:
            lead = next(
                (
                    (p.a or p.b).text.strip()
                    for p in self.pairs
                    if (p.a or p.b).text.strip()
                ),
                "",
            )
            return lead[:110] or f"Section {self.marked}"
        block = self.a_block or self.b_block
        if block.kind == "paragraph":
            return block.text[:110]
        if self.heading:
            return self.heading[:110]
        labels = [r.label for r in block.rows if r.label and not r.is_figure_row]
        if labels:
            return max(labels, key=len)[:110]
        return (block.labels[0][:110] if block.labels else "Table")

    @property
    def pages(self) -> tuple[str, str]:
        return (
            self.a_block.pages if self.a_block else "—",
            self.b_block.pages if self.b_block else "—",
        )

    def regions(self, side: str) -> dict:
        """Bounding box per page of this section's content on one side.

        Used to draw the section's outline and serial number onto that
        document, so the report and the marked-up PDF point at each other.
        """
        boxes: dict = {}
        for pair in self.pairs:
            unit = pair.a if side == "a" else pair.b
            if unit is None:
                continue
            for row in unit.rows:
                box = boxes.get(row.page)
                boxes[row.page] = (
                    row.bbox
                    if box is None
                    else (
                        min(box[0], row.x0),
                        min(box[1], row.y0),
                        max(box[2], row.x1),
                        max(box[3], row.y1),
                    )
                )
        return boxes

    def first_page(self, side: str) -> int | None:
        pages = self.regions(side)
        return min(pages) if pages else None


def group(pairs: list[Pair]) -> list[Section]:
    """Collect pairs into sections, using document A's structure as the spine.

    A short run of rows existing only in B joins the section it was inserted
    into, rather than splitting that section in two around it. Without this, one
    document's table arrives as rubble whenever the other inserts a line into
    it — and B, which reflows, inserts many.
    """
    # A line repeated on page after page is a running header ("INFOSYS LIMITED
    # AND SUBSIDIARIES"), not the heading of whatever follows it.
    seen = Counter(
        _norm((p.a or p.b).text)
        for p in pairs
        if (p.a or p.b).kind == "paragraph"
    )
    running = {text for text, n in seen.items() if n > _RUNNING_HEADER}

    sections: list[Section] = []
    heading = ""
    for index, pair in enumerate(pairs):
        current = sections[-1] if sections else None
        current_key = getattr(current, "_key", None) if current is not None else None

        marked = (pair.a or pair.b).section
        if marked is not None:
            # A numbered section is one section in the report, whatever blocks
            # the two documents happen to have split it into.
            key = ("mark", marked)
        elif pair.a is not None:
            key = ("A", pair.a.block_index)
        else:
            key = ("B", pair.b.block_index)
            # Absorb the insertion only if the document returns to the same
            # block soon afterwards; a long run of B-only material is content in
            # its own right, not an interruption. Kind is not required to match:
            # a wide table's labels wrap onto their own lines in the narrower
            # document, and those continuation lines carry no figures, so they
            # arrive as prose in the middle of a table.
            if (
                current is not None
                and current.a_block is not None
                and getattr(current, "_key", (None,))[0] != "mark"
            ):
                resumes = next(
                    (p for p in pairs[index + 1 : index + 1 + _MAX_INSERTION] if p.a),
                    None,
                )
                if resumes is not None and ("A", resumes.a.block_index) == current_key:
                    key = current_key

        if current is not None and current_key == key:
            current.pairs.append(pair)
            if current.b_block is None and pair.b is not None:
                current.b_block = pair.b.block
            continue

        section = Section(
            kind=(pair.a or pair.b).block.kind,
            a_block=pair.a.block if pair.a is not None else None,
            b_block=pair.b.block if pair.b is not None else None,
            pairs=[pair],
            heading=heading,
            marked=marked,
        )
        section._key = key
        section.serial = len(sections) + 1
        sections.append(section)

        # Remember the heading the following tables sit under, so they can be
        # named. Only genuine headings update it: a wide table's wrapped column
        # labels also arrive as short paragraphs, and letting those win would
        # title the balance sheet after a stray word from its own header.
        if section.kind == "paragraph":
            text = (pair.a or pair.b).text.strip()
            if _norm(text) not in running:
                found = _heading_of(text)
                if found:
                    heading = found
    return sections


@dataclass
class Summary:
    units_a: int
    units_b: int
    matched: int
    changed: int
    only_in_a: int
    only_in_b: int
    paragraphs_matched: int
    rows_matched: int
    changed_figures: int
    unchanged: int


def summarise(pairs: list[Pair], units_a, units_b) -> Summary:
    both = [p for p in pairs if p.a is not None and p.b is not None]
    return Summary(
        units_a=len(units_a),
        units_b=len(units_b),
        matched=len(both),
        changed=sum(1 for p in both if p.changed),
        only_in_a=sum(1 for p in pairs if p.b is None),
        only_in_b=sum(1 for p in pairs if p.a is None),
        paragraphs_matched=sum(1 for p in both if p.kind == "paragraph"),
        rows_matched=sum(1 for p in both if p.kind == "row"),
        changed_figures=sum(len(p.changed_figures) for p in both),
        # Matched and found to agree. Not the same as the anchor count, which is
        # an internal detail of how the pairing was reached.
        unchanged=sum(1 for p in both if not p.changed),
    )
