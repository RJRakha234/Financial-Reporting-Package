"""Compare the text of a reference PDF against a scanned copy of the same document.

A common integrity question for a financial report is *"does this scanned /
printed-and-rescanned copy actually say the same thing as the original PDF?"* —
a single altered digit (an ``8`` that became a ``3``) or a dropped line is easy
to miss by eye. This module answers it:

1. **Extract** the text of each document. A text-based PDF is read directly with
   ``pdfplumber``; a *scanned* page (an image with no text layer) is read with
   OCR (``pytesseract`` over the page rendered by PyMuPDF). OCR only kicks in for
   pages that carry no extractable text, so a searchable PDF needs no OCR at all.
2. **Diff the words** most-different-first, reporting exactly which passages were
   inserted, deleted, or changed, plus an overall similarity score.
3. **Diff the numbers** on top of the word diff. Every figure is parsed with the
   same financial-number logic the footing checker uses (thousands separators,
   parenthesised negatives, currency symbols/codes, nil dashes), and a figure
   present in one document but not the other — or an altered figure of the same
   length — is reported on its own. This is the payload that matters for
   financial statements, where a corrupted digit changes a number, not a word.

Like the rest of fincheck this runs **entirely offline**: text comes from
``pdfplumber``/PyMuPDF and OCR from a locally installed ``tesseract`` engine.
Nothing is uploaded anywhere.
"""

import difflib
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .numbers import CURRENCY_SYMBOLS, parse_number

# File extensions treated as a single scanned image (always OCR'd).
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
# A PDF page with fewer than this many extracted characters is treated as a
# scanned image and sent to OCR when ``ocr="auto"``.
_SPARSE_CHARS = 12

_DASH_RE = re.compile("[–—−‒]")
_WS_RE = re.compile(r"\s+")
# A space sitting between a digit and a following group of exactly three digits
# is a thousands separator ("1 234 567" -> "1234567"), so numbers line up.
_THOUSANDS_SPACE_RE = re.compile(r"(?<=\d) (?=\d\d\d\b)")
# A number as it appears in a statement: optional currency/paren/sign, digits
# with comma grouping, optional decimals. Parsed by ``parse_number`` afterwards.
_NUMBER_TOKEN_RE = re.compile(
    r"\(?[" + re.escape(CURRENCY_SYMBOLS) + r"]?-?\d[\d,]*(?:\.\d+)?\)?"
)


class OcrUnavailable(RuntimeError):
    """Raised when a page needs OCR but the OCR stack is not installed."""


@dataclass
class Segment:
    """A passage that differs between the two documents.

    ``tag`` is ``"replace"`` (text changed), ``"delete"`` (present in the
    reference but missing from the scan), or ``"insert"`` (extra text in the
    scan). ``reference``/``scanned`` hold the differing words on each side.
    """

    tag: str
    reference: str
    scanned: str


@dataclass
class NumberChange:
    """A figure that does not match between the two documents.

    ``kind`` is ``"changed"`` (a figure of the same length reads differently —
    the classic OCR digit swap), ``"missing_in_scan"`` (a figure in the
    reference the scan does not contain), or ``"extra_in_scan"`` (a figure the
    scan has that the reference does not).
    """

    kind: str
    reference: float | None
    scanned: float | None


@dataclass
class ComparisonResult:
    reference_path: str
    scanned_path: str
    reference_pages: int
    scanned_pages: int
    similarity: float
    segments: list[Segment] = field(default_factory=list)
    number_changes: list[NumberChange] = field(default_factory=list)
    ocr_used: bool = False

    @property
    def identical(self) -> bool:
        """True when no textual or numeric difference was found."""
        return not self.segments and not self.number_changes

    def as_dict(self) -> dict:
        from .report import comparison_to_dict

        return comparison_to_dict(self)

    def as_json(self) -> str:
        from .report import comparison_to_json

        return comparison_to_json(self)


def normalize(text: str, ignore_case: bool = True) -> str:
    """Collapse OCR-irrelevant noise so the word diff sees real differences.

    Unicode is folded (NFKC), the various dashes are unified, runs of
    whitespace become a single space, and — by default — case is dropped, since
    a scan's capitalisation is not a meaningful change.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _DASH_RE.sub("-", text)
    text = _WS_RE.sub(" ", text).strip()
    return text.lower() if ignore_case else text


def _extract_numbers(text: str) -> list[float]:
    text = _THOUSANDS_SPACE_RE.sub("", text)
    values: list[float] = []
    for match in _NUMBER_TOKEN_RE.finditer(text):
        value = parse_number(match.group())
        if value is not None:
            values.append(round(value, 2))
    return values


def _digit_length(value: float) -> int:
    return len(str(int(abs(round(value)))))


def _pair_number_changes(
    missing: list[float], extra: list[float]
) -> tuple[list[tuple[float, float]], list[float], list[float]]:
    """Pair a missing figure with an extra one of the same digit length.

    After the multiset subtraction only genuinely differing figures remain, so a
    reference figure and a scanned figure with the same number of digits are
    almost always the same figure with a corrupted digit; pairing them surfaces
    the change as a single ``reference -> scanned`` line. Anything left unpaired
    is reported as purely missing or purely extra.
    """
    extra_by_length: dict[int, list[float]] = defaultdict(list)
    for value in extra:
        extra_by_length[_digit_length(value)].append(value)
    for bucket in extra_by_length.values():
        bucket.sort()

    changed: list[tuple[float, float]] = []
    leftover_missing: list[float] = []
    for ref_value in sorted(missing):
        bucket = extra_by_length.get(_digit_length(ref_value))
        if bucket:
            i = min(range(len(bucket)), key=lambda k: abs(bucket[k] - ref_value))
            changed.append((ref_value, bucket.pop(i)))
        else:
            leftover_missing.append(ref_value)

    leftover_extra = [v for bucket in extra_by_length.values() for v in bucket]
    return changed, leftover_missing, leftover_extra


def _compare_numbers(reference_text: str, scanned_text: str) -> list[NumberChange]:
    reference = Counter(_extract_numbers(reference_text))
    scanned = Counter(_extract_numbers(scanned_text))
    missing = list((reference - scanned).elements())
    extra = list((scanned - reference).elements())

    changed, missing, extra = _pair_number_changes(missing, extra)
    result = [NumberChange("changed", ref, scan) for ref, scan in changed]
    result += [NumberChange("missing_in_scan", v, None) for v in sorted(missing)]
    result += [NumberChange("extra_in_scan", None, v) for v in sorted(extra)]
    return result


def compare_texts(
    reference_text: str, scanned_text: str, ignore_case: bool = True
) -> tuple[float, list[Segment], list[NumberChange]]:
    """Compare two already-extracted strings.

    Returns ``(similarity, segments, number_changes)`` where ``similarity`` is a
    word-level ratio in ``[0, 1]``. Kept separate from document extraction so the
    comparison logic is testable without any PDF or OCR.
    """
    ref_words = normalize(reference_text, ignore_case).split()
    scan_words = normalize(scanned_text, ignore_case).split()

    matcher = difflib.SequenceMatcher(a=ref_words, b=scan_words, autojunk=False)
    similarity = matcher.ratio()

    segments: list[Segment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        segments.append(
            Segment(
                tag=tag,
                reference=" ".join(ref_words[i1:i2]),
                scanned=" ".join(scan_words[j1:j2]),
            )
        )

    # Numbers are compared on the raw text so parsing sees the real digits,
    # currency symbols, and parentheses rather than the case-folded words.
    number_changes = _compare_numbers(reference_text, scanned_text)
    return similarity, segments, number_changes


def _lazy_ocr():
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise OcrUnavailable(
            "OCR is needed to read a scanned page but the OCR stack is not "
            "available. Install it with 'pip install pytesseract Pillow' and "
            "make sure the tesseract engine is on your PATH "
            "(e.g. 'apt-get install tesseract-ocr' or 'brew install tesseract'). "
            "If your scanned document already has a text layer, pass "
            "--ocr never to skip OCR entirely."
        ) from exc
    return pytesseract, Image


def _ocr_image_file(path: str, lang: str) -> str:
    pytesseract, Image = _lazy_ocr()
    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang=lang)


def _ocr_pdf_pages(
    path: str, indices: list[int], lang: str, dpi: int
) -> dict[int, str]:
    import io

    import fitz  # PyMuPDF

    pytesseract, Image = _lazy_ocr()
    out: dict[int, str] = {}
    doc = fitz.open(path)
    try:
        for i in indices:
            pixmap = doc[i].get_pixmap(dpi=dpi)
            with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                out[i] = pytesseract.image_to_string(image, lang=lang)
    finally:
        doc.close()
    return out


def _extract_pdf_text(
    path: str, ocr: str, lang: str, dpi: int
) -> tuple[list[str], bool]:
    import pdfplumber

    pages: list[str] = []
    sparse: list[int] = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            pages.append(text)
            if len(text) < _SPARSE_CHARS:
                sparse.append(i)

    if ocr == "always":
        targets = list(range(len(pages)))
    elif ocr == "auto":
        targets = sparse
    else:  # "never"
        targets = []

    ocr_used = False
    if targets:
        for i, text in _ocr_pdf_pages(path, targets, lang, dpi).items():
            if text.strip():
                pages[i] = text.strip()
                ocr_used = True
    return pages, ocr_used


def extract_document_text(
    path: str, ocr: str = "auto", lang: str = "eng", dpi: int = 300
) -> tuple[list[str], bool]:
    """Return ``(page_texts, ocr_used)`` for a PDF, image, or plain-text file.

    ``ocr`` controls OCR of a PDF: ``"auto"`` (default) OCRs only pages with no
    text layer, ``"always"`` OCRs every page, ``"never"`` disables OCR. Image
    files are always OCR'd.
    """
    if ocr not in ("auto", "always", "never"):
        raise ValueError(f"ocr must be auto/always/never, got {ocr!r}")

    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_EXTS:
        return [_ocr_image_file(path, lang)], True
    if suffix == ".pdf":
        return _extract_pdf_text(path, ocr, lang, dpi)

    # Fall back to reading a plain-text file (handy for tests and .txt exports).
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [fh.read()], False


def compare_documents(
    reference: str,
    scanned: str,
    ocr: str = "auto",
    lang: str = "eng",
    dpi: int = 300,
    ignore_case: bool = True,
) -> ComparisonResult:
    """Compare a reference document against a scanned copy.

    Args:
        reference: path to the text-based reference (usually the original PDF).
        scanned: path to the scanned copy (an image PDF, an image, or a PDF that
            already carries an OCR text layer).
        ocr: OCR policy for pages with no text layer (see
            :func:`extract_document_text`).
        lang: tesseract language code(s), e.g. ``"eng"`` or ``"eng+deu"``.
        dpi: render resolution used when a page must be OCR'd.
        ignore_case: fold case before diffing the words (default ``True``).
    """
    ref_pages, ref_ocr = extract_document_text(reference, ocr, lang, dpi)
    scan_pages, scan_ocr = extract_document_text(scanned, ocr, lang, dpi)

    similarity, segments, number_changes = compare_texts(
        "\n".join(ref_pages), "\n".join(scan_pages), ignore_case=ignore_case
    )
    return ComparisonResult(
        reference_path=reference,
        scanned_path=scanned,
        reference_pages=len(ref_pages),
        scanned_pages=len(scan_pages),
        similarity=similarity,
        segments=segments,
        number_changes=number_changes,
        ocr_used=ref_ocr or scan_ocr,
    )
