"""Compare common notes across documents and produce reviewable differences.

Pipeline:

1. Load each document into canonical-topic-keyed notes (``Document``).
2. Build an **alignment matrix**: which canonical notes appear in which docs.
3. For every note shared by a pair of documents, diff the two prose streams on
   their neutralized fingerprints (robust to wrapping and to the expected
   entity/framework/currency variation), then project each differing region
   back onto the *original* prose so the checker reads real text. Adjacent
   regions are coalesced and trivially short ones dropped.

Each :class:`Difference` carries a stable hash of its content so a reviewer's
accept/ignore decision can be remembered across quarters (see ``ledger.py``):
if the underlying wording changes, the hash changes and the difference
re-surfaces for review.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
from dataclasses import dataclass, field

import os

from ..extract import extract_pages
from .normalize import DocKind, canonical_topic, infer_kind, normalize_with_map
from .sections import Section, narrative_text, split_into_sections

# Number of leading pages whose raw text feeds document-kind inference (cover /
# primary statements, where framework / scope / currency are stated plainly).
_KIND_INFER_PAGES = 6

# A differing region shorter than this many fingerprint characters (summed over
# both sides) is treated as incidental noise and not reported.
_MIN_DIFF_CHARS = 6
# Differing regions separated by fewer than this many matching characters are
# merged into one, so a reviewer sees one coherent change rather than shards.
_COALESCE_GAP = 12
# Readable context shown either side of a difference, in source characters.
_CONTEXT = 48


@dataclass
class Document:
    """One financial statement, indexed by canonical note topic."""

    name: str
    kind: DocKind
    notes: dict[str, Section] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.name} — {self.kind.label}"


@dataclass
class Span:
    """A region of one document's prose involved in a difference."""

    text: str  # the differing fragment (original, readable)
    before: str  # shared context immediately before
    after: str  # shared context immediately after


@dataclass
class Difference:
    """One substantive wording difference between two documents in one note."""

    topic: str
    title: str
    left_doc: str
    right_doc: str
    kind: str  # "replace" | "insert" | "delete"
    left: Span
    right: Span

    @property
    def hash(self) -> str:
        h = hashlib.sha1()
        h.update(
            "\x1f".join(
                [self.topic, self.left_doc, self.right_doc, self.left.text, self.right.text]
            ).encode("utf-8")
        )
        return h.hexdigest()[:16]


@dataclass
class PairComparison:
    left_doc: str
    right_doc: str
    similarity: float
    differences: list[Difference]


@dataclass
class ComparisonResult:
    documents: list[Document]
    # canonical topic -> {doc name -> note title present in that doc}
    matrix: dict[str, dict[str, str]]
    pairs: list[PairComparison]

    @property
    def common_topics(self) -> list[str]:
        n = len(self.documents)
        return [t for t, present in self.matrix.items() if len(present) == n]

    @property
    def all_differences(self) -> list[Difference]:
        return [d for p in self.pairs for d in p.differences]


def load_document(path: str, name: str | None = None, kind: DocKind | None = None) -> Document:
    """Load a PDF into a :class:`Document`, inferring its kind if not given."""
    pages = extract_pages(path)
    sections = split_into_sections(pages)
    if kind is None:
        head = " ".join(
            r.label for p in pages[:_KIND_INFER_PAGES] for r in p.rows
        )
        kind = infer_kind(head)
    notes: dict[str, Section] = {}
    for sec in sections:
        topic = canonical_topic(sec.title)
        # If a topic appears twice (rare), keep the one with more prose.
        if topic not in notes or len(narrative_text(sec)) > len(narrative_text(notes[topic])):
            notes[topic] = sec
    return Document(name=name or os.path.basename(path), kind=kind, notes=notes)


def _expand_to_word(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen ``[start, end)`` outward to the nearest whitespace word boundaries."""
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def _coalesce(opcodes: list[tuple]) -> list[tuple[int, int, int, int]]:
    """Merge non-equal opcodes separated by short equal runs into diff blocks."""
    blocks: list[list[int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            if i2 - i1 >= _COALESCE_GAP:
                blocks.append(None)  # hard boundary
            continue
        if blocks and blocks[-1] is not None:
            b = blocks[-1]
            b[1], b[3] = i2, j2
        else:
            blocks.append([i1, i2, j1, j2])
    return [tuple(b) for b in blocks if b is not None]


def _span(text: str, lo: int, hi: int) -> Span:
    # A zero-width span (one side of an insert/delete) has no text of its own —
    # expanding it would wrongly swallow a neighbouring word, so only gather the
    # surrounding context to show *where* the other side's text would go.
    if hi > lo:
        lo, hi = _expand_to_word(text, lo, hi)
        fragment = text[lo:hi].strip()
    else:
        fragment = ""
    before = text[max(0, lo - _CONTEXT) : lo].strip()
    after = text[hi : hi + _CONTEXT].strip()
    return Span(text=fragment, before=before, after=after)


def diff_notes(
    topic: str,
    title: str,
    left_doc: str,
    left_text: str,
    right_doc: str,
    right_text: str,
) -> tuple[float, list[Difference]]:
    """Diff two notes' prose; return (similarity, list of differences)."""
    a_norm, a_src = normalize_with_map(left_text)
    b_norm, b_src = normalize_with_map(right_text)
    sm = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=False)
    similarity = sm.ratio()

    diffs: list[Difference] = []
    for i1, i2, j1, j2 in _coalesce(sm.get_opcodes()):
        if (i2 - i1) + (j2 - j1) < _MIN_DIFF_CHARS:
            continue
        # Project fingerprint indices back onto the original source text.
        a_lo = a_src[i1] if i1 < len(a_src) else len(left_text)
        a_hi = (a_src[i2 - 1] + 1) if i2 > i1 else a_lo
        b_lo = b_src[j1] if j1 < len(b_src) else len(right_text)
        b_hi = (b_src[j2 - 1] + 1) if j2 > j1 else b_lo
        kind = "replace" if i2 > i1 and j2 > j1 else ("delete" if i2 > i1 else "insert")
        diffs.append(
            Difference(
                topic=topic,
                title=title,
                left_doc=left_doc,
                right_doc=right_doc,
                kind=kind,
                left=_span(left_text, a_lo, a_hi),
                right=_span(right_text, b_lo, b_hi),
            )
        )
    return similarity, diffs


def compare_documents(documents: list[Document]) -> ComparisonResult:
    """Compare every pair of documents over their shared notes."""
    matrix: dict[str, dict[str, str]] = {}
    for doc in documents:
        for topic, sec in doc.notes.items():
            matrix.setdefault(topic, {})[doc.name] = sec.title

    pairs: list[PairComparison] = []
    for left, right in itertools.combinations(documents, 2):
        shared = sorted(set(left.notes) & set(right.notes))
        differences: list[Difference] = []
        sims: list[float] = []
        for topic in shared:
            lt = narrative_text(left.notes[topic])
            rt = narrative_text(right.notes[topic])
            if not lt and not rt:
                continue
            title = left.notes[topic].title
            sim, diffs = diff_notes(topic, title, left.name, lt, right.name, rt)
            sims.append(sim)
            differences.extend(diffs)
        pairs.append(
            PairComparison(
                left_doc=left.name,
                right_doc=right.name,
                similarity=sum(sims) / len(sims) if sims else 1.0,
                differences=differences,
            )
        )

    # Order the matrix: notes common to all documents first, then by topic.
    n = len(documents)
    ordered = dict(
        sorted(matrix.items(), key=lambda kv: (len(kv[1]) != n, kv[0]))
    )
    return ComparisonResult(documents=documents, matrix=ordered, pairs=pairs)
