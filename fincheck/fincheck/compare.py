"""Exact comparison of two PDFs.

The rest of ``fincheck`` reconstructs tables from geometry, which necessarily
involves judgement calls (which words share a row, which figures share a
column). Comparing two PDFs does **not** need any of that, and this module
deliberately makes none of those calls.

A PDF page is, at the file level, an ordered set of drawing operations: show
this string, in this font, at this point; stroke this line; paint this image.
Those operations are what the file actually contains, so comparing them is
comparison of the documents themselves rather than of a reconstruction. Every
layer below is an exact equality test over such operations:

``bytes``
    SHA-256 of each file. Equal means the same file, full stop.
``geometry``
    Page count, and each page's width, height and rotation.
``text``
    Every text span the file draws — its string, font, size, colour, style
    flags and bounding box — compared as a multiset. A span is the PDF's own
    unit (one show-text operation with uniform styling), not a grouping this
    module invents.
``graphics``
    Every vector path (the ruling lines and boxes of a statement) and every
    embedded image, compared by their drawing parameters and content hash.
``pixels``
    Both pages rendered at the same resolution and compared byte for byte.

None of these layers guesses. What they cannot do is say *which line item* a
changed figure belongs to: a PDF has no rows, columns or line items, only
glyphs at coordinates, so any such statement is a reconstruction. This module
therefore reports a changed figure as what it verifiably is — this string, at
this point, became that string — and marks the nearby label it prints for
convenience as the inference it is.

Coordinates are compared exactly by default. ``position_tolerance`` quantises
them if you need to ignore sub-point drift from a re-export.
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field

import fitz  # PyMuPDF

from .numbers import parse_number

DEFAULT_DPI = 150

# Coordinates round to this many decimals when comparing exactly, which removes
# float-repr noise without merging any position a PDF can actually distinguish.
_EXACT_NDIGITS = 6
# Pixels per coarse block when locating a changed region within a scanline.
_SCAN_BLOCK = 64
# Scanlines that differ and are no further apart than this join one region.
_BAND_GAP = 4


def _quantiser(position_tolerance: float):
    """Return a function mapping a coordinate to its comparison key."""
    if position_tolerance <= 0:
        return lambda v: round(float(v), _EXACT_NDIGITS)
    return lambda v: round(float(v) / position_tolerance) * position_tolerance


# --------------------------------------------------------------------------
# Exact records of what a page draws
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TextSpan:
    """One show-text operation: a string drawn in one style at one place."""

    text: str
    font: str
    size: float
    color: int
    flags: int
    # Where the text operator put the pen: the baseline start. Unlike the
    # bounding box this does not shift when the font changes, so it is the
    # stable way to say "the same place".
    ox: float
    oy: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def style(self) -> tuple:
        return (self.font, self.size, self.color, self.flags)

    @property
    def position(self) -> tuple:
        """Where the text was placed. Deliberately not the bounding box, which
        also moves when the font changes without the text going anywhere."""
        return (self.ox, self.oy)

    @property
    def value(self) -> float | None:
        return parse_number(self.text)

    def where(self) -> str:
        return f"({self.ox:g}, {self.oy:g})"


@dataclass(frozen=True)
class ImageBox:
    """An embedded image, identified by the hash of its own bytes."""

    digest: str
    width: int
    height: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass(frozen=True)
class VectorPath:
    """A stroked/filled path — the ruling lines and boxes of a statement."""

    items: tuple
    stroke: tuple | None
    fill: tuple | None
    width: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class PageContent:
    index: int
    width: float
    height: float
    rotation: int
    spans: tuple[TextSpan, ...]
    images: tuple[ImageBox, ...]
    paths: tuple[VectorPath, ...]

    @property
    def geometry(self) -> tuple[float, float, int]:
        return (self.width, self.height, self.rotation)

    @property
    def text(self) -> str:
        """Page text in canonical order: by text baseline, then along it.

        Sorting is a canonicalisation, not a layout model — it exists so two
        pages drawing the same strings produce the same output string whatever
        order the content stream emits them in. Nothing is grouped into rows or
        columns, and the sort keys are the baseline coordinates the file
        itself carries.
        """
        ordered = sorted(self.spans, key=lambda s: (s.oy, s.ox))
        return "".join(s.text for s in ordered)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(repr(self.geometry).encode())
        for group in (
            sorted(self.spans, key=lambda s: (s.oy, s.ox, s.text)),
            sorted(self.images, key=lambda i: (i.y0, i.x0, i.digest)),
            sorted(self.paths, key=lambda p: (p.y0, p.x0, repr(p.items))),
        ):
            for record in group:
                h.update(repr(record).encode())
        return h.hexdigest()


def _norm_color(color) -> tuple | None:
    if color is None:
        return None
    return tuple(round(float(c), 4) for c in color)


def _norm_path_item(item, q) -> tuple:
    """Flatten one path element to plain rounded numbers."""
    parts: list = [item[0]]
    for operand in item[1:]:
        if isinstance(operand, fitz.Point):
            parts += [q(operand.x), q(operand.y)]
        elif isinstance(operand, fitz.Rect):
            parts += [q(operand.x0), q(operand.y0), q(operand.x1), q(operand.y1)]
        elif isinstance(operand, fitz.Quad):
            for point in (operand.ul, operand.ur, operand.ll, operand.lr):
                parts += [q(point.x), q(point.y)]
        elif isinstance(operand, (int, float)):
            parts.append(q(operand))
        else:
            parts.append(str(operand))
    return tuple(parts)


def read_page(page: "fitz.Page", index: int, q) -> PageContent:
    spans: list[TextSpan] = []
    images: list[ImageBox] = []

    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 0:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    x0, y0, x1, y1 = span["bbox"]
                    ox, oy = span.get("origin", (x0, y1))
                    spans.append(
                        TextSpan(
                            text=span["text"],
                            font=span.get("font", ""),
                            size=round(float(span.get("size", 0.0)), 4),
                            color=int(span.get("color", 0)),
                            flags=int(span.get("flags", 0)),
                            ox=q(ox),
                            oy=q(oy),
                            x0=q(x0),
                            y0=q(y0),
                            x1=q(x1),
                            y1=q(y1),
                        )
                    )
        else:
            x0, y0, x1, y1 = block["bbox"]
            payload = block.get("image")
            digest = (
                hashlib.sha256(payload).hexdigest()
                if isinstance(payload, (bytes, bytearray))
                else ""
            )
            images.append(
                ImageBox(
                    digest=digest,
                    width=int(block.get("width", 0)),
                    height=int(block.get("height", 0)),
                    x0=q(x0),
                    y0=q(y0),
                    x1=q(x1),
                    y1=q(y1),
                )
            )

    paths: list[VectorPath] = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        paths.append(
            VectorPath(
                items=tuple(
                    _norm_path_item(i, q) for i in drawing.get("items", [])
                ),
                stroke=_norm_color(drawing.get("color")),
                fill=_norm_color(drawing.get("fill")),
                width=round(float(drawing.get("width") or 0.0), 4),
                x0=q(rect.x0),
                y0=q(rect.y0),
                x1=q(rect.x1),
                y1=q(rect.y1),
            )
        )

    return PageContent(
        index=index,
        width=q(page.rect.width),
        height=q(page.rect.height),
        rotation=int(page.rotation),
        spans=tuple(spans),
        images=tuple(images),
        paths=tuple(paths),
    )


def read_pages(doc: "fitz.Document", position_tolerance: float = 0.0):
    q = _quantiser(position_tolerance)
    return [read_page(page, i, q) for i, page in enumerate(doc)]


# --------------------------------------------------------------------------
# Differences
# --------------------------------------------------------------------------


@dataclass
class SpanChange:
    """One text difference.

    ``kind`` is ``added``, ``removed``, ``edited`` (same place and style, new
    string), ``moved``, ``restyled`` or ``moved+restyled``. Pairing an removed
    span with an added one is presentation only — the underlying difference is
    the exact multiset difference either way.
    """

    kind: str
    before: TextSpan | None = None
    after: TextSpan | None = None
    context: str = ""

    @property
    def is_numeric(self) -> bool:
        return (
            self.kind == "edited"
            and self.before is not None
            and self.after is not None
            and self.before.value is not None
            and self.after.value is not None
        )

    @property
    def delta(self) -> float | None:
        if not self.is_numeric:
            return None
        return self.after.value - self.before.value


@dataclass
class Region:
    """A rectangle, in PDF points, covering pixels that differ."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class PixelDiff:
    dpi: int
    identical: bool
    comparable: bool = True
    changed_pixels: int = 0
    total_pixels: int = 0
    regions: list[Region] = field(default_factory=list)
    note: str = ""

    @property
    def changed_fraction(self) -> float:
        return self.changed_pixels / self.total_pixels if self.total_pixels else 0.0


@dataclass
class PageComparison:
    page_a: int | None
    page_b: int | None
    geometry_equal: bool = True
    geometry_note: str = ""
    text_equal: bool = True
    spans_equal: bool = True
    span_changes: list[SpanChange] = field(default_factory=list)
    images_added: int = 0
    images_removed: int = 0
    paths_added: int = 0
    paths_removed: int = 0
    pixels: PixelDiff | None = None

    @property
    def only_in_one(self) -> bool:
        return self.page_a is None or self.page_b is None

    @property
    def graphics_equal(self) -> bool:
        return not (
            self.images_added
            or self.images_removed
            or self.paths_added
            or self.paths_removed
        )

    @property
    def identical(self) -> bool:
        return (
            not self.only_in_one
            and self.geometry_equal
            and self.spans_equal
            and self.graphics_equal
            and (self.pixels is None or self.pixels.identical)
        )

    @property
    def numeric_changes(self) -> list[SpanChange]:
        return [c for c in self.span_changes if c.is_numeric]


@dataclass
class ComparisonResult:
    pdf_a: str
    pdf_b: str
    sha256_a: str
    sha256_b: str
    page_count_a: int
    page_count_b: int
    pages: list[PageComparison]
    position_tolerance: float = 0.0
    dpi: int | None = None
    aligned: bool = False

    @property
    def bytes_identical(self) -> bool:
        return self.sha256_a == self.sha256_b

    @property
    def page_counts_equal(self) -> bool:
        return self.page_count_a == self.page_count_b

    @property
    def geometry_identical(self) -> bool:
        return self.page_counts_equal and all(
            p.geometry_equal and not p.only_in_one for p in self.pages
        )

    @property
    def text_identical(self) -> bool:
        return self.page_counts_equal and all(
            p.text_equal and not p.only_in_one for p in self.pages
        )

    @property
    def spans_identical(self) -> bool:
        """Every string is drawn in the same style at the same place."""
        return self.page_counts_equal and all(
            p.spans_equal and not p.only_in_one for p in self.pages
        )

    @property
    def graphics_identical(self) -> bool:
        """Every vector path and embedded image is the same."""
        return self.page_counts_equal and all(
            p.graphics_equal and not p.only_in_one for p in self.pages
        )

    @property
    def content_identical(self) -> bool:
        """Every drawing operation on every page is the same."""
        return (
            self.page_counts_equal
            and self.geometry_identical
            and all(
                p.spans_equal and p.graphics_equal and not p.only_in_one
                for p in self.pages
            )
        )

    @property
    def pixels_compared(self) -> bool:
        return self.dpi is not None and any(p.pixels is not None for p in self.pages)

    @property
    def visually_identical(self) -> bool | None:
        """``None`` when rendering was skipped, so callers cannot mistake it."""
        if not self.pixels_compared:
            return None
        if not self.page_counts_equal:
            return False
        return all(
            p.pixels is not None and p.pixels.identical for p in self.pages
        )

    @property
    def identical(self) -> bool:
        return self.bytes_identical or (
            self.content_identical and self.visually_identical is not False
        )

    @property
    def changed_pages(self) -> list[PageComparison]:
        return [p for p in self.pages if not p.identical]

    @property
    def numeric_changes(self) -> list[tuple[PageComparison, SpanChange]]:
        return [(p, c) for p in self.pages for c in p.numeric_changes]

    def as_dict(self) -> dict:
        from .compare_report import comparison_to_dict

        return comparison_to_dict(self)

    def as_json(self) -> str:
        from .compare_report import comparison_to_json

        return comparison_to_json(self)


# --------------------------------------------------------------------------
# Text diff
# --------------------------------------------------------------------------


def _sort_key(span: TextSpan) -> tuple:
    return (span.oy, span.ox, span.text)


def _pair_on(only_a: list, only_b: list, key) -> tuple[list, list, list]:
    """Pair removals with additions sharing ``key``; return pairs and leftovers."""
    buckets: dict = {}
    for span in only_b:
        buckets.setdefault(key(span), []).append(span)
    for bucket in buckets.values():
        bucket.sort(key=_sort_key)

    pairs, unpaired = [], []
    for span in only_a:
        bucket = buckets.get(key(span))
        if bucket:
            pairs.append((span, bucket.pop(0)))
        else:
            unpaired.append(span)
    remaining = [s for bucket in buckets.values() for s in bucket]
    remaining.sort(key=_sort_key)
    return pairs, unpaired, remaining


def diff_spans(a: PageContent, b: PageContent) -> list[SpanChange]:
    """Exact multiset difference between two pages' text spans.

    Everything reported is a real difference; the ``kind`` labels merely pair
    removals with additions so the output reads as edits rather than as twice
    as many unrelated lines.
    """
    only_a = sorted((Counter(a.spans) - Counter(b.spans)).elements(), key=_sort_key)
    only_b = sorted((Counter(b.spans) - Counter(a.spans)).elements(), key=_sort_key)
    if not only_a and not only_b:
        return []

    changes: list[SpanChange] = []
    shifted: list[tuple] = []

    # Unchanged text that slid up or down its column, which is what inserting or
    # deleting a row does to everything below it. Matching these first matters:
    # otherwise each shifted row pairs with whatever now sits at its old
    # baseline, and one inserted line reads as a cascade of edits. Keying on the
    # string, its style and its column edge keeps the pairing tight.
    for column in (lambda s: (s.text, s.style, s.ox), lambda s: (s.text, s.style, s.x1)):
        pairs, only_a, only_b = _pair_on(only_a, only_b, column)
        shifted += pairs

    # An edit in place: same baseline and style, different string. Financial
    # figures are right-aligned, so anchor on the right edge as well as the
    # left — a shorter number keeps x1 and moves its origin.
    for anchor in (
        lambda s: (s.oy, s.ox, s.style),
        lambda s: (s.oy, s.x1, s.style),
    ):
        pairs, only_a, only_b = _pair_on(only_a, only_b, anchor)
        changes += [
            SpanChange(kind="edited", before=before, after=after)
            for before, after in pairs
        ]

    # Whatever is left that still shares a string: moved further afield, or
    # restyled, or both.
    pairs, only_a, only_b = _pair_on(only_a, only_b, lambda s: s.text)
    for before, after in shifted + pairs:
        moved = before.position != after.position
        restyled = before.style != after.style
        kind = (
            "moved+restyled" if moved and restyled else "moved" if moved else "restyled"
        )
        changes.append(SpanChange(kind=kind, before=before, after=after))

    changes += [SpanChange(kind="removed", before=s) for s in only_a]
    changes += [SpanChange(kind="added", after=s) for s in only_b]

    for change in changes:
        change.context = _context_for(change, a, b)

    changes.sort(key=lambda c: _sort_key(c.after or c.before))
    return changes


def _context_for(change: SpanChange, a: PageContent, b: PageContent) -> str:
    """Nearest non-numeric text sharing a baseline, as a reading aid only.

    Sharing a baseline is exact — it is the same coordinate in the file — but
    reading that as "the same row of the same table" is an inference, and it is
    the only one in this module. It never affects any equality verdict.
    """
    span = change.after or change.before
    page = b if change.after is not None else a
    if span is None:
        return ""
    same_line = [
        s
        for s in page.spans
        if s is not span
        and s.oy == span.oy
        and s.text.strip()
        and parse_number(s.text) is None
    ]
    if not same_line:
        return ""
    nearest = min(same_line, key=lambda s: abs(s.x0 - span.x0))
    return nearest.text.strip()


# --------------------------------------------------------------------------
# Pixel diff
# --------------------------------------------------------------------------


def _row_changed_range(row_a: bytes, row_b: bytes, n: int, width: int):
    """First and last differing pixel index in a scanline.

    Only the two boundary blocks are examined pixel by pixel; the blocks
    between them cannot move the first or last index, so scanning them would
    cost time without changing the answer.
    """
    step = _SCAN_BLOCK * n
    starts = range(0, width * n, step)
    differing = [
        s for s in starts if row_a[s : s + step] != row_b[s : s + step]
    ]
    if not differing:
        return None, None

    def refine(start: int, reverse: bool):
        offsets = range(0, min(step, width * n - start), n)
        for offset in reversed(offsets) if reverse else offsets:
            if (
                row_a[start + offset : start + offset + n]
                != row_b[start + offset : start + offset + n]
            ):
                return (start + offset) // n
        return start // n

    return refine(differing[0], False), refine(differing[-1], True)


# Maps every non-zero byte to 0xFF, so channel planes can be OR-ed together as
# one big integer instead of pixel by pixel in Python.
_NONZERO_TO_FF = bytes(0 if i == 0 else 0xFF for i in range(256))


def _count_changed_pixels(a: bytes, b: bytes, n: int) -> int:
    """Exact number of pixels that differ, computed without a Python loop.

    XOR-ing the two buffers as single integers marks every differing byte;
    folding the channel planes together with OR then marks every differing
    pixel, and ``bytes.count`` totals them. Every step runs in C, which matters
    because the alternative — touching each pixel from Python — takes minutes
    on a page that changed substantially.
    """
    diff = (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(
        len(a), "big"
    )
    if n == 1:
        return len(diff) - diff.count(0)
    acc = 0
    for channel in range(n):
        acc |= int.from_bytes(diff[channel::n].translate(_NONZERO_TO_FF), "big")
    folded = acc.to_bytes(len(diff) // n, "big")
    return len(folded) - folded.count(0)


def _pixel_diff(page_a, page_b, dpi: int) -> PixelDiff:
    pix_a = page_a.get_pixmap(dpi=dpi, alpha=False)
    pix_b = page_b.get_pixmap(dpi=dpi, alpha=False)

    if (pix_a.width, pix_a.height) != (pix_b.width, pix_b.height):
        return PixelDiff(
            dpi=dpi,
            identical=False,
            comparable=False,
            note=(
                f"page sizes differ: {pix_a.width}x{pix_a.height} vs "
                f"{pix_b.width}x{pix_b.height} pixels at {dpi} dpi"
            ),
        )

    samples_a, samples_b = pix_a.samples, pix_b.samples
    total = pix_a.width * pix_a.height
    if samples_a == samples_b:
        return PixelDiff(dpi=dpi, identical=True, total_pixels=total)

    scale = 72.0 / dpi
    stride, n, width = pix_a.stride, pix_a.n, pix_a.width
    row_bytes = width * n

    # Counted over the padding-free rows so the stride cannot inflate the total.
    if stride == row_bytes:
        changed_pixels = _count_changed_pixels(samples_a, samples_b, n)
    else:
        changed_pixels = sum(
            _count_changed_pixels(
                samples_a[y * stride : y * stride + row_bytes],
                samples_b[y * stride : y * stride + row_bytes],
                n,
            )
            for y in range(pix_a.height)
        )

    bands: list[list[int]] = []  # [y_top, y_bottom, x_first, x_last]
    for y in range(pix_a.height):
        row_a = samples_a[y * stride : y * stride + row_bytes]
        row_b = samples_b[y * stride : y * stride + row_bytes]
        if row_a == row_b:
            continue
        first, last = _row_changed_range(row_a, row_b, n, width)
        if first is None:
            continue
        if bands and y - bands[-1][1] <= _BAND_GAP:
            band = bands[-1]
            band[1] = y
            band[2] = min(band[2], first)
            band[3] = max(band[3], last)
        else:
            bands.append([y, y, first, last])

    regions = [
        Region(
            x0=x_first * scale,
            y0=y_top * scale,
            x1=(x_last + 1) * scale,
            y1=(y_bottom + 1) * scale,
        )
        for y_top, y_bottom, x_first, x_last in bands
    ]
    return PixelDiff(
        dpi=dpi,
        identical=False,
        changed_pixels=changed_pixels,
        total_pixels=total,
        regions=regions,
    )


# --------------------------------------------------------------------------
# Page alignment
# --------------------------------------------------------------------------


def _align_pages(a: list[PageContent], b: list[PageContent]) -> list[tuple]:
    """Longest-common-subsequence alignment on exact page content hashes.

    Strict positional pairing is the default because it assumes nothing. This
    is offered for the case where a page was inserted or removed: pages that
    match are matched exactly (identical content hash), and the alignment
    itself is a stated, deterministic choice rather than a hidden guess.
    """
    hashes_a = [p.content_hash for p in a]
    hashes_b = [p.content_hash for p in b]
    rows, cols = len(a), len(b)
    table = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(rows - 1, -1, -1):
        for j in range(cols - 1, -1, -1):
            table[i][j] = (
                table[i + 1][j + 1] + 1
                if hashes_a[i] == hashes_b[j]
                else max(table[i + 1][j], table[i][j + 1])
            )

    pairs: list[tuple] = []
    i = j = 0
    while i < rows and j < cols:
        if hashes_a[i] == hashes_b[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif table[i + 1][j] >= table[i][j + 1]:
            pairs.append((i, None))
            i += 1
        else:
            pairs.append((None, j))
            j += 1
    pairs += [(k, None) for k in range(i, rows)]
    pairs += [(None, k) for k in range(j, cols)]
    return pairs


def _strict_pairs(a: list[PageContent], b: list[PageContent]) -> list[tuple]:
    pairs = [(i, i) for i in range(min(len(a), len(b)))]
    pairs += [(i, None) for i in range(len(b), len(a))]
    pairs += [(None, j) for j in range(len(a), len(b))]
    return pairs


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_pdfs(
    pdf_a: str,
    pdf_b: str,
    dpi: int | None = DEFAULT_DPI,
    position_tolerance: float = 0.0,
    align_pages: bool = False,
) -> ComparisonResult:
    """Compare two PDFs exactly.

    Args:
        pdf_a: the earlier/reference PDF.
        pdf_b: the later/candidate PDF.
        dpi: render both and compare pixels at this resolution; ``None`` skips
            rendering, which leaves ``visually_identical`` undetermined.
        position_tolerance: quantise coordinates before comparing them. ``0``
            (the default) compares them exactly.
        align_pages: pair pages by matching content instead of by position.
            Off by default, because positional pairing assumes nothing.
    """
    sha_a, sha_b = _sha256(pdf_a), _sha256(pdf_b)

    doc_a = fitz.open(pdf_a)
    doc_b = fitz.open(pdf_b)
    try:
        content_a = read_pages(doc_a, position_tolerance)
        content_b = read_pages(doc_b, position_tolerance)

        pairs = (
            _align_pages(content_a, content_b)
            if align_pages
            else _strict_pairs(content_a, content_b)
        )

        pages: list[PageComparison] = []
        for index_a, index_b in pairs:
            if index_a is None or index_b is None:
                pages.append(
                    PageComparison(
                        page_a=index_a,
                        page_b=index_b,
                        geometry_equal=False,
                        geometry_note="page present in only one document",
                        text_equal=False,
                        spans_equal=False,
                    )
                )
                continue

            page_a, page_b = content_a[index_a], content_b[index_b]
            comparison = PageComparison(page_a=index_a, page_b=index_b)

            comparison.geometry_equal = page_a.geometry == page_b.geometry
            if not comparison.geometry_equal:
                comparison.geometry_note = (
                    f"{page_a.width:g}x{page_a.height:g} rot {page_a.rotation} vs "
                    f"{page_b.width:g}x{page_b.height:g} rot {page_b.rotation}"
                )

            comparison.text_equal = page_a.text == page_b.text
            comparison.span_changes = diff_spans(page_a, page_b)

            images_a, images_b = Counter(page_a.images), Counter(page_b.images)
            comparison.images_removed = sum((images_a - images_b).values())
            comparison.images_added = sum((images_b - images_a).values())
            paths_a, paths_b = Counter(page_a.paths), Counter(page_b.paths)
            comparison.paths_removed = sum((paths_a - paths_b).values())
            comparison.paths_added = sum((paths_b - paths_a).values())

            comparison.spans_equal = not comparison.span_changes

            if dpi is not None:
                comparison.pixels = _pixel_diff(
                    doc_a[index_a], doc_b[index_b], dpi
                )

            pages.append(comparison)

        return ComparisonResult(
            pdf_a=pdf_a,
            pdf_b=pdf_b,
            sha256_a=sha_a,
            sha256_b=sha_b,
            page_count_a=len(content_a),
            page_count_b=len(content_b),
            pages=pages,
            position_tolerance=position_tolerance,
            dpi=dpi,
            aligned=align_pages,
        )
    finally:
        doc_a.close()
        doc_b.close()
