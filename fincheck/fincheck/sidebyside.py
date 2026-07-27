"""Render an aligned, side-by-side HTML comparison of two documents.

Paragraphs sit beside their counterparts with a word-level diff; table rows sit
beside theirs with each figure compared column by column. Rows are laid out as
one HTML table per section so the two sides stay locked together vertically —
a section wide enough to need scrolling scrolls as a unit, never one side
against the other.

The structure being shown is reconstructed (see :mod:`fincheck.blocks`), so the
page states that plainly. It is a reviewer's worksheet, not evidence.
"""

import html
from dataclasses import dataclass, field
from typing import NamedTuple

from .align import Pair, Section, Summary
from .numbers import format_number


@dataclass
class Meta:
    pdf_a: str
    pdf_b: str
    pages_a: int
    pages_b: int
    summary: Summary
    label_a: str = "A"
    label_b: str = "B"
    producer_a: str = ""
    producer_b: str = ""
    marked_sections: int = 0
    # Relative hrefs to the numbered copies, so every page reference in the
    # report can open the page it was drawn from.
    marked_href_a: str = ""
    marked_href_b: str = ""
    # Passages and figures marked on each copy, so the report can state its
    # own coverage rather than leave it to be taken on trust.
    coverage_a: tuple = (0, 0)
    coverage_b: tuple = (0, 0)
    # Page heights in points, page number -> height. PDF view coordinates run
    # bottom-up, the extraction's run top-down; the heights convert between
    # them so a link can open a copy scrolled to the exact passage.
    heights_a: dict = field(default_factory=dict)
    heights_b: dict = field(default_factory=dict)
    # Rendered pages of the marked copies (see sidemarks.render_previews), so
    # the report can show the highlighted source in place — external PDF
    # viewers cannot be relied on to honour a link's page-and-position.
    previews_a: dict = field(default_factory=dict)
    previews_b: dict = field(default_factory=dict)
    # Alignment-independent figure reconciliation (see fincheck.ledger).
    ledger: object = None

    @property
    def made_a(self) -> str:
        return produced_by(self.producer_a)

    @property
    def made_b(self) -> str:
        return produced_by(self.producer_b)

    @property
    def tags(self) -> "Tags":
        return Tags(
            short_a=_short_tag(self.label_a),
            short_b=_short_tag(self.label_b),
            full_a=self.label_a,
            full_b=self.label_b,
            href_a=self.marked_href_a,
            href_b=self.marked_href_b,
            heights_a=self.heights_a,
            heights_b=self.heights_b,
        )


class Tags(NamedTuple):
    """Column names in two lengths.

    Stacked figure rows are prefixed dozens of times per table, so they need a
    tag narrow enough not to eat the width the figures need. The sticky column
    header carries the full name, so the short form only has to be recognisable.
    """

    short_a: str
    short_b: str
    full_a: str
    full_b: str
    href_a: str = ""
    href_b: str = ""
    heights_a: dict = {}
    heights_b: dict = {}


_SHORT = (
    ("excel", "XLS"),
    ("html", "HTML"),
    ("word", "DOC"),
    ("latex", "TEX"),
)


def _short_tag(label: str) -> str:
    low = label.lower()
    for needle, short in _SHORT:
        if needle in low:
            return short
    words = [w for w in label.replace("_", " ").replace("-", " ").split() if w]
    if len(words) > 1:
        return "".join(w[0] for w in words[:4]).upper()
    return (words[0][:4] if words else label[:4]).upper()


# How a PDF was made, kept as a subtitle under the filename. It is genuinely
# useful — "the Excel one" and "the Word one" is how people talk about two
# renderings of the same content — but it is not what anyone recognises a file
# by, so it does not get to be the heading.
_PRODUCERS = (
    ("excel", "Excel export"),
    ("skia", "HTML print"),
    ("chrome", "HTML print"),
    ("wkhtmltopdf", "HTML print"),
    ("word", "Word export"),
    ("indesign", "InDesign"),
    ("latex", "LaTeX"),
    ("distiller", "Distiller"),
    ("ghostscript", "Ghostscript"),
)


def default_label(path: str, producer: str = "", creator: str = "") -> str:
    """Name a column after the file, which is what the reader recognises."""
    stem = path.rsplit("/", 1)[-1]
    return stem[:-4] if stem.lower().endswith(".pdf") else stem


def produced_by(producer: str, creator: str = "") -> str:
    """A short description of how the PDF was made, or ``""``."""
    haystack = f"{producer} {creator}".lower()
    for needle, label in _PRODUCERS:
        if needle in haystack:
            return label
    return producer.strip()




def _e(text) -> str:
    return html.escape(str(text))


# A destination sits this many points above the passage it opens, so the
# passage lands just under the top of the window rather than glued to it.
_SPOT_LEAD = 28


def _url(href: str, page, top=None) -> str:
    """A link into the marked copy: the page, and the exact spot when known.

    ``#page=N&zoom=100,left,top`` is the PDF open-parameter syntax understood
    by Acrobat, Chromium and pdf.js alike; ``top`` is in PDF coordinates
    (bottom-up), which is what the heights in :class:`Meta` are for.
    """
    fragment = f"{href}#page={page}"
    if top is not None:
        fragment += f"&zoom=100,0,{int(top)}"
    return _e(fragment)


def _unit_spot(unit, heights: dict):
    """(page, top) for the first line of a unit, or ``None``."""
    if unit is None or not unit.rows:
        return None
    row = unit.rows[0]
    height = heights.get(row.page)
    top = max(0, int(height - row.y0 + _SPOT_LEAD)) if height else None
    return (row.page, top)


def _unit_rect(unit):
    """(page, x0, y0, x1, y1) around the unit's rows on its first page."""
    if unit is None or not unit.rows:
        return None
    page = unit.rows[0].page
    rows = [r for r in unit.rows if r.page == page]
    return (
        page,
        min(r.x0 for r in rows),
        min(r.y0 for r in rows),
        max(r.x1 for r in rows),
        max(r.y1 for r in rows),
    )


def _peek_attr(side: str, unit) -> str:
    """Data the in-page preview needs to spotlight this passage."""
    rect = _unit_rect(unit)
    if rect is None:
        return ""
    page, x0, y0, x1, y1 = rect
    return f' data-peek="{side}:{page}:{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}"'


def _section_peek(section, side: str) -> str:
    regions = section.regions(side)
    if not regions:
        return ""
    page, box = sorted(regions.items())[0]
    return (
        f' data-peek="{side}:{page}:'
        f'{box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f}"'
    )


def _section_spot(section, side: str, heights: dict):
    """(page, top) for the first marked region of a section, or ``None``."""
    regions = section.regions(side)
    if not regions:
        return None
    page, box = sorted(regions.items())[0]
    height = heights.get(page)
    top = max(0, int(height - box[1] + _SPOT_LEAD)) if height else None
    return (page, top)


def _page_link(
    page: str, href: str, css: str = "", top=None, peek: str = ""
) -> str:
    """A page number that opens the marked-up PDF at that page."""
    text = _e(page)
    if not href or not page or page == "—":
        return text
    first = str(page).split("\u2013")[0].split("-")[0].strip()
    if not first.isdigit():
        return text
    return (
        f'<a class="pl {css}" href="{_url(href, first, top)}"{peek} target="_blank" '
        f'rel="noopener" title="open page {_e(first)} of the marked-up PDF">{text}</a>'
    )


def _spot_link(unit, href: str, heights: dict, side: str) -> str:
    """The unit's page number, linking to its exact spot in the marked copy."""
    if unit is None:
        return ""
    spot = _unit_spot(unit, heights)
    if spot is None or not href:
        return _e(unit.page)
    page, top = spot
    return _page_link(str(page), href, top=top, peek=_peek_attr(side, unit))


def _loc(text_html: str, unit, href: str, heights: dict, side: str) -> str:
    """Wrap rendered cell content so it opens its own highlighted source."""
    if not href:
        return text_html
    spot = _unit_spot(unit, heights)
    if spot is None:
        return text_html
    page, top = spot
    return (
        f'<a class="loc" href="{_url(href, page, top)}"{_peek_attr(side, unit)} '
        f'target="_blank" rel="noopener" '
        f'title="show this passage in its highlighted source">'
        f"{text_html}</a>"
    )


def _marked(words, side: str) -> str:
    """One side of a word diff, with every differing word marked.

    ``side`` is ``"a"`` for the benchmark or ``"b"`` for the compared
    document. Each side shows only what applies to it — the benchmark shows
    what was struck, the other what was added — so the two read as documents
    rather than as a merged diff.
    """
    out = []
    for op, text in words:
        if not text:
            continue
        if op == "=":
            out.append(_e(text))
        elif op == "~-" and side == "a":
            out.append(f'<u class="fmt">{_e(text)}</u>')
        elif op == "~+" and side == "b":
            out.append(f'<u class="fmt">{_e(text)}</u>')
        elif op == ">-" and side == "a":
            out.append(f'<span class="mv">{_e(text)}</span>')
        elif op == ">+" and side == "b":
            out.append(f'<span class="mv">{_e(text)}</span>')
        elif op == "-" and side == "a":
            out.append(f"<del>{_e(text)}</del>")
        elif op == "+" and side == "b":
            out.append(f"<ins>{_e(text)}</ins>")
    return " ".join(out)


def _label_html(pair, unit, side: str) -> str:
    """A row label, word-marked when the two sides word it differently."""
    if pair.words and pair.a is not None and pair.b is not None:
        marked = _marked(pair.words, side)
        if marked.strip():
            return marked
    return _e(unit.text) or "&nbsp;"


def _figure_cells(values, changed, missing: int = 0) -> str:
    """Figure spans; a changed cell names both readings in its tooltip.

    ``changed`` may be a plain set of indices, or a dict mapping the index to
    the ``(benchmark, compared)`` values so every deviating cell can say what
    should have been there without leaving the page.
    """
    cells = []
    for i, v in enumerate(values):
        title = ""
        if i in changed and isinstance(changed, dict):
            va, vb = changed[i]
            side_a = format_number(va) if va is not None else "absent"
            side_b = format_number(vb) if vb is not None else "absent"
            title = f' title="benchmark: {side_a} · compared: {side_b}"'
        cls = " fig--changed" if i in changed else ""
        cells.append(
            f'<span class="fig{cls}"{title}>{_e(format_number(v))}</span>'
        )
    return "".join(cells) + '<span class="fig fig--absent">—</span>' * missing


def _tracked_figs(pair: Pair) -> str:
    """One run of figure cells with each deviation shown as old-struck, new-inserted."""
    a, b = pair.a, pair.b
    if a is None:
        return "".join(
            f'<span class="fig"><ins>{_e(format_number(v))}</ins></span>'
            for v in b.values
        )
    if b is None:
        return "".join(
            f'<span class="fig"><del>{_e(format_number(v))}</del></span>'
            for v in a.values
        )
    changed = {i: (va, vb) for i, va, vb in pair.changed_figures}
    cells = []
    for i in range(max(len(a.values), len(b.values))):
        if i in changed:
            va, vb = changed[i]
            inner = ""
            if va is not None:
                inner += f"<del>{_e(format_number(va))}</del>"
            if vb is not None:
                inner += f"<ins>{_e(format_number(vb))}</ins>"
            cells.append(f'<span class="fig fig--changed">{inner}</span>')
        else:
            v = a.values[i] if i < len(a.values) else b.values[i]
            cells.append(f'<span class="fig">{_e(format_number(v))}</span>')
    return "".join(cells)


def _tracked_words(words) -> str:
    """A word diff as one tracked-changes pane: kept, struck and inserted."""
    out = []
    for op, text in words:
        if not text:
            continue
        if op == "=":
            out.append(_e(text))
        elif op in ("~-", "~+"):
            out.append(f'<u class="fmt">{_e(text)}</u>')
        elif op in (">-", ">+"):
            out.append(f'<span class="mv">{_e(text)}</span>')
        elif op == "-":
            out.append(f"<del>{_e(text)}</del>")
        else:
            out.append(f"<ins>{_e(text)}</ins>")
    return " ".join(out)


def _tracked_label(pair: Pair, tags: Tags) -> str:
    """The row label as tracked changes, marked word by word.

    Striking the whole label and inserting the whole replacement was correct
    but unreadable: on a row where one word changed it painted the entire line
    twice and said nothing about which word it was.
    """
    a, b = pair.a, pair.b
    if a is None:
        return f"<ins>{_e(b.text) or '&nbsp;'}</ins>"
    if b is None:
        return f"<del>{_e(a.text) or '&nbsp;'}</del>"
    if pair.words and a.text != b.text:
        marked = _tracked_words(pair.words)
        if marked.strip():
            return marked
    if pair.status == "label-differs" and a.text != b.text:
        return f"<del>{_e(a.text)}</del> <ins>{_e(b.text)}</ins>"
    return _loc(_e(a.text) or "&nbsp;", a, tags.href_a, tags.heights_a, "a")


# Beyond this many figure columns, two side-by-side copies cannot both fit on
# screen, so A is stacked over B instead. Reading down a column beats scrolling
# sideways to find its counterpart.
WIDE_TABLE_COLUMNS = 6


def _stacked_row_html(pair: Pair, tags: Tags) -> str:
    """One row with the two documents stacked, columns aligned, for a wide table."""
    changed = {i: (va, vb) for i, va, vb in pair.changed_figures}
    a, b = pair.a, pair.b
    status = pair.status

    def line(unit, other, tag: str, letter: str) -> str:
        if unit is None:
            return (
                f'<span class="ln ln-{letter} ln--absent"><em>{tag}</em>'
                f'<span class="absent">not present</span></span>'
            )
        gap = 0
        if other is not None and len(unit.values) < len(other.values):
            gap = len(other.values) - len(unit.values)
        return (
            f'<span class="ln ln-{letter}"><em>{tag}</em>'
            f'<span class="figs">{_figure_cells(unit.values, changed, gap)}</span>'
            f"</span>"
        )

    label_side = "a" if a is not None else "b"
    label = _label_html(pair, a or b, label_side)
    label_cls = "label label--changed" if status == "label-differs" else "label"
    alt = ""
    if status == "label-differs" and a is not None and b is not None:
        alt = f'<span class="alt">{_e(tags.full_b)}: {_e(b.text)}</span>'
    tracked = (
        f'<span class="ln tracked"><em>&Delta;</em>'
        f'<span class="figs">{_tracked_figs(pair)}</span></span>'
    )

    return (
        f'<tr class="r r--{status}" title="match {pair.score}%">'
        f'<td class="gut gut-a">{_spot_link(a, tags.href_a, tags.heights_a, "a")}</td>'
        f'<td class="stack">'
        f'<span class="{label_cls}">'
        f'{_loc(label, a or b, tags.href_a if a else tags.href_b, tags.heights_a if a else tags.heights_b, label_side)}'
        f"</span>{alt}"
        f'{line(a, b, tags.short_a, "a")}{line(b, a, tags.short_b, "b")}{tracked}'
        f"</td>"
        f'<td class="gut gut-b">{_spot_link(b, tags.href_b, tags.heights_b, "b")}</td>'
        f"</tr>"
    )


def _row_pair_html(pair: Pair, tags: Tags) -> str:
    changed = {i: (va, vb) for i, va, vb in pair.changed_figures}
    status = pair.status
    a, b = pair.a, pair.b

    def side(unit, other, href: str, heights: dict, letter: str) -> str:
        if unit is None:
            return (
                f'<td class="side side-{letter} side--empty">'
                '<span class="absent">not present</span></td>'
            )
        label_cls = "label"
        if status == "label-differs":
            label_cls += " label--changed"
        gap = 0
        if other is not None and len(unit.values) < len(other.values):
            gap = len(other.values) - len(unit.values)
        return (
            f'<td class="side side-{letter}">'
            f'<span class="{label_cls}">'
            f'{_loc(_label_html(pair, unit, letter), unit, href, heights, letter)}</span>'
            f'<span class="figs">{_figure_cells(unit.values, changed, gap)}</span>'
            f"</td>"
        )

    tracked = (
        f'<td class="side tracked">'
        f'<span class="label">{_tracked_label(pair, tags)}</span>'
        f'<span class="figs">{_tracked_figs(pair)}</span></td>'
    )
    return (
        f'<tr class="r r--{status}" title="match {pair.score}%">'
        f'<td class="gut gut-a">{_spot_link(a, tags.href_a, tags.heights_a, "a")}</td>'
        f'{side(a, b, tags.href_a, tags.heights_a, "a")}'
        f'{side(b, a, tags.href_b, tags.heights_b, "b")}'
        f"{tracked}"
        f'<td class="gut gut-b">{_spot_link(b, tags.href_b, tags.heights_b, "b")}</td>'
        f"</tr>"
    )


def _paragraph_html(pair: Pair, tags: Tags) -> str:
    a, b = pair.a, pair.b
    gut_a = f'<div class="gut gut-a">{_spot_link(a, tags.href_a, tags.heights_a, "a")}</div>'
    gut_b = f'<div class="gut gut-b">{_spot_link(b, tags.href_b, tags.heights_b, "b")}</div>'
    if a is not None and b is not None and not pair.words:
        # A table row that landed in a prose section: no word diff was computed,
        # so show both sides plainly rather than rendering two empty paragraphs.
        left = (
            f'{_loc(_e(a.text), a, tags.href_a, tags.heights_a, "a")}'
            f'<span class="figs">{_figure_cells(a.values, set())}</span>'
        )
        right = (
            f'{_loc(_e(b.text), b, tags.href_b, tags.heights_b, "b")}'
            f'<span class="figs">{_figure_cells(b.values, set())}</span>'
        )
        return (
            f'<div class="prose prose--{pair.status}" title="match {pair.score}%">'
            f"{gut_a}"
            f'<div class="side side-a"><p class="para">{left}</p></div>'
            f'<div class="side side-b"><p class="para">{right}</p></div>'
            f'<div class="side tracked"><p class="para">{left}</p></div>'
            f"{gut_b}</div>"
        )
    if a is None or b is None:
        only = a or b
        side_letter = "a" if a is not None else "b"
        href = tags.href_a if a is not None else tags.href_b
        heights = tags.heights_a if a is not None else tags.heights_b
        linked = _loc(_e(only.text), only, href, heights, side_letter)
        text = f'<p class="para">{linked}</p>'
        mark = "del" if a is not None else "ins"
        tracked = (
            f'<div class="side tracked"><p class="para">'
            f"<{mark}>{linked}</{mark}></p></div>"
        )
        empty = '<div class="side side-{0} side--empty"><span class="absent">not present</span></div>'
        left = f'<div class="side side-a">{text}</div>' if a is not None else empty.format("a")
        right = f'<div class="side side-b">{text}</div>' if b is not None else empty.format("b")
        return (
            f'<div class="prose prose--{pair.status}" title="match {pair.score}%">'
            f"{gut_a}{left}{right}{tracked}"
            f"{gut_b}</div>"
        )

    left_parts, right_parts, tracked_parts = [], [], []
    for op, text in pair.words:
        if not text:
            continue
        if op == "=":
            left_parts.append(_e(text))
            right_parts.append(_e(text))
            tracked_parts.append(_e(text))
        elif op == "~-":
            # Same words, set differently — punctuation, spacing, a bullet
            # glyph. Marked quietly, and each side keeps its own text.
            left_parts.append(f'<u class="fmt">{_e(text)}</u>')
            tracked_parts.append(f'<u class="fmt">{_e(text)}</u>')
        elif op == "~+":
            right_parts.append(f'<u class="fmt">{_e(text)}</u>')
        elif op == ">-":
            # Same words, elsewhere in the passage. Not an edit.
            left_parts.append(f'<span class="mv">{_e(text)}</span>')
            tracked_parts.append(f'<span class="mv">{_e(text)}</span>')
        elif op == ">+":
            right_parts.append(f'<span class="mv">{_e(text)}</span>')
        elif op == "-":
            left_parts.append(f"<del>{_e(text)}</del>")
            tracked_parts.append(f"<del>{_e(text)}</del>")
        else:
            right_parts.append(f"<ins>{_e(text)}</ins>")
            tracked_parts.append(f"<ins>{_e(text)}</ins>")

    return (
        f'<div class="prose prose--{pair.status}" title="match {pair.score}%">'
        f"{gut_a}"
        f'<div class="side side-a"><p class="para">{" ".join(left_parts)}</p></div>'
        f'<div class="side side-b"><p class="para">{" ".join(right_parts)}</p></div>'
        f'<div class="side tracked"><p class="para">{" ".join(tracked_parts)}</p></div>'
        f"{gut_b}</div>"
    )


def _collapse_wrapping(pairs: list[Pair], render, tags: Tags) -> list[str]:
    """Render a table's rows, folding away wrapped-label artefacts.

    A wide table set in a narrower page wraps its row labels onto extra lines.
    Those lines carry no figures and exist on one side only, so they say nothing
    about the numbers — but there can be dozens, and left in they bury the rows
    that matter. They are counted, not dropped.
    """
    out: list[str] = []
    run = 0

    def flush():
        nonlocal run
        if run:
            out.append(
                f'<tr class="r r--wrap"><td class="gut"></td>'
                f'<td class="wrapnote" colspan="3">{run} wrapped label line'
                f'{"s" if run != 1 else ""} on one side only</td>'
                f'<td class="gut"></td></tr>'
            )
            run = 0

    for pair in pairs:
        one_sided = pair.a is None or pair.b is None
        unit = pair.a or pair.b
        if one_sided and not unit.values and len(unit.text) <= 60:
            run += 1
            continue
        flush()
        out.append(render(pair, tags))
    flush()
    return out


_STATUS_LABEL = {
    "same": "matches benchmark",
    "formatting": "formatting only",
    "reordered": "same words, reordered",
    "changed": "deviates",
    "added": "not in benchmark",
    "removed": "benchmark only",
}


def _ledger_panel(ledger, label_a: str, label_b: str) -> str:
    """The reconciliation that owes nothing to the alignment.

    Placed above the section detail because it answers the question a
    reconciliation exists to answer — was any figure lost or invented — with no
    similarity judgement anywhere in the chain.
    """
    if ledger is None:
        return ""
    from .ledger import Ledger

    def rows(items, side_label) -> str:
        amounts, _ = Ledger._split(items)
        if not amounts:
            return ""
        out = []
        for item in amounts[:40]:
            first = item.occurrences[0]
            note = (
                '<span class="labsent">absent</span>'
                if item.absent
                else f'<span class="lcount">{item.here}&times; vs {item.there}&times;</span>'
            )
            out.append(
                f'<tr><td class="lv">{_e(format_number(item.value))}</td>'
                f'<td class="lc">{note}</td>'
                f'<td class="lp">p{first.page}</td>'
                f'<td class="lx">{_e(first.context)}</td></tr>'
            )
        more = (
            f'<tr><td colspan="4" class="lmore">and {len(amounts) - 40:,} '
            "more amount(s)</td></tr>"
            if len(amounts) > 40
            else ""
        )
        return (
            f'<h3 class="lh">{_e(side_label)}</h3>'
            f'<div class="scroll"><table class="ledger">'
            f"<thead><tr><th>Amount</th><th>Reading</th><th>Page</th>"
            f"<th>Printed on this line</th></tr></thead>"
            f"<tbody>{''.join(out)}{more}</tbody></table></div>"
        )

    missing = rows(ledger.only_in_a, f"Surplus in {label_a} (the benchmark)")
    extra = rows(ledger.only_in_b, f"Surplus in {label_b}")
    clean = not missing and not extra
    state = "good" if clean else "bad"
    absent = ledger.absent_amounts
    if clean:
        verdict = (
            "Every amount printed in one document is printed in the other, the "
            "same number of times."
        )
    elif absent:
        verdict = (
            f"{len(absent)} amount(s) printed in the benchmark appear "
            "<strong>nowhere</strong> in the compared document. Stated without "
            "any pairing, so this does not depend on the matching below."
        )
    else:
        verdict = (
            "Every amount appears in both documents, but some are printed a "
            "different number of times. Stated without any pairing, so this "
            "does not depend on the matching below."
        )
    return (
        f'<section class="assure assure--{state}" id="ledger">'
        f'<div class="ahead"><h2>Independent figure reconciliation</h2>'
        f'<span class="acov">{ledger.coverage:.2f}% of the benchmark&rsquo;s '
        f"figures found</span></div>"
        f'<p class="sub">Every numeric token on every page of both files, '
        f"reconciled as two multisets — no paragraphs, no rows, no similarity. "
        f"It cannot tell you a figure moved; it can tell you, without "
        f"qualification, whether one was lost or invented. "
        f"{ledger.total_a:,} figures in the benchmark, "
        f"{ledger.total_b:,} in the compared document.</p>"
        f'<p class="averdict">{verdict}</p>'
        f"{missing}{extra}"
        f"</section>"
    )


def _tier(score: int) -> str:
    """CSS class for a composite score, following the reviewer's bands."""
    if score == 100:
        return "t-exact"
    if score >= 75:
        return "t-high"
    if score >= 50:
        return "t-part"
    return "t-low"


def _score_chip(score: int) -> str:
    return f'<span class="scorechip {_tier(score)}">{score}%</span>'


def _section_html(section: Section, index: int, tags: Tags) -> str:
    pages_a, pages_b = section.pages
    status = section.status
    label = _STATUS_LABEL.get(status, status)
    kind = "Table" if section.kind == "table" else "Text"
    # Only meaningful where both sides exist; on a one-sided section every pair
    # counts as changed, and "1 of 1 differ" would just restate the chip.
    detail = (
        f"{section.changed} of {len(section.pairs)} deviate"
        if status == "changed"
        else ""
    )

    badge = f'<span class="serial">{section.serial}</span>'
    if section.marked is not None:
        badge += f'<span class="snum">&sect;{_e(section.marked)}</span>'
    # A section that deviates needs a decision; one that agrees does not.
    needs_decision = status in ("changed", "added", "removed")
    decide = (
        f'<span class="decide" data-serial="{section.serial}">'
        f'<button class="dbtn dbtn--acc" data-decide="accepted" '
        f'title="Accept this variation">Accept</button>'
        f'<button class="dbtn dbtn--rej" data-decide="rejected" '
        f'title="Reject: needs correction">Reject</button>'
        f'<span class="dstate"></span></span>'
        if needs_decision
        else ""
    )
    views = (
        '<span class="secviews" role="group" aria-label="view this section as">'
        '<button data-sview="sbs" title="Side by side">Side</button>'
        '<button data-sview="tracked" title="Tracked changes">Track</button>'
        '<button data-sview="before" title="Benchmark as written">Before</button>'
        '<button data-sview="after" title="Compared as written">After</button>'
        "</span>"
    )
    spot_a = _section_spot(section, "a", tags.heights_a)
    spot_b = _section_spot(section, "b", tags.heights_b)
    moved_chip = (
        '<span class="chip chip--moved">moved</span>' if section.moved else ""
    )
    head = (
        f'<div class="shead">'
        f'{badge}'
        f'<span class="skind">{kind}</span>'
        f'<h3>{_e(section.title) or "&nbsp;"}</h3>'
        f'<span class="spages">{_page_link(pages_a, tags.href_a, top=spot_a[1] if spot_a else None, peek=_section_peek(section, "a"))}'
        f' &middot; {_page_link(pages_b, tags.href_b, top=spot_b[1] if spot_b else None, peek=_section_peek(section, "b"))}</span>'
        f"{_score_chip(section.score)}"
        f'<span class="chip chip--{status}">{label}</span>'
        f"{moved_chip}"
        f'{f"<span class=sdetail>{detail}</span>" if detail else ""}'
        f"{views}{decide}"
        f"</div>"
    )

    if section.kind == "table":
        columns = max(
            max(len(p.a.values) if p.a else 0, len(p.b.values) if p.b else 0)
            for p in section.pairs
        )
        wide = columns > WIDE_TABLE_COLUMNS
        render = _stacked_row_html if wide else _row_pair_html
        body = (
            f'<div class="scroll"><table class="rows rows--{"stacked" if wide else "cols"}">'
            + "".join(_collapse_wrapping(section.pairs, render, tags))
            + "</table></div>"
        )
    else:
        body = "".join(_paragraph_html(p, tags) for p in section.pairs)

    marked_cls = " sec--marked" if section.marked is not None else " sec--unmarked"
    needs = " sec--needs" if needs_decision else ""
    return (
        f'<section class="sec sec--{status}{marked_cls}{needs}" id="s{index}"'
        f' data-serial="{section.serial}" data-score="{section.score}">'
        f"{head}{body}</section>"
    )


_CSS = """
:root {
  --paper:#FBFAF7; --panel:#F3F2ED; --ink:#16191A; --ink-soft:#4A514D;
  --muted:#6C736E; --rule:#C8CEC9; --rule-soft:#E4E7E2; --accent:#0E5A56;
  --differs:#9B3220; --differs-bg:#F6E3DE; --same:#2C6A4E;
  --add:#1F6F4A; --add-bg:#DFF0E5; --del:#9B3220; --del-bg:#F8E4DF;
  --bench:#8A6B2E; --bench-bg:#F5EEDC;
  --warn:#7E6410; --warn-bg:#F3EDCC;
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#121614; --panel:#191F1D; --ink:#E7EAE7; --ink-soft:#B3BAB6;
  --muted:#8C948F; --rule:#2E3835; --rule-soft:#222A28; --accent:#63B4AB;
  --differs:#E08A72; --differs-bg:#3A211B; --same:#79C7A0;
  --add:#79C7A0; --add-bg:#16301F; --del:#E08A72; --del-bg:#361D18;
  --bench:#CFA95F; --bench-bg:#2C2415;
  --warn:#D6C05E; --warn-bg:#2E2A15;
}}
:root[data-theme=dark]{
  --paper:#121614; --panel:#191F1D; --ink:#E7EAE7; --ink-soft:#B3BAB6;
  --muted:#8C948F; --rule:#2E3835; --rule-soft:#222A28; --accent:#63B4AB;
  --differs:#E08A72; --differs-bg:#3A211B; --same:#79C7A0;
  --add:#79C7A0; --add-bg:#16301F; --del:#E08A72; --del-bg:#361D18;
  --bench:#CFA95F; --bench-bg:#2C2415;
  --warn:#D6C05E; --warn-bg:#2E2A15;
}
:root[data-theme=light]{
  --paper:#FBFAF7; --panel:#F3F2ED; --ink:#16191A; --ink-soft:#4A514D;
  --muted:#6C736E; --rule:#C8CEC9; --rule-soft:#E4E7E2; --accent:#0E5A56;
  --differs:#9B3220; --differs-bg:#F6E3DE; --same:#2C6A4E;
  --add:#1F6F4A; --add-bg:#DFF0E5; --del:#9B3220; --del-bg:#F8E4DF;
  --bench:#8A6B2E; --bench-bg:#F5EEDC;
  --warn:#7E6410; --warn-bg:#F3EDCC;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55}
.wrap{max-width:88rem;margin:0 auto;padding:2.75rem 1.25rem 5rem}
/* ---- hero: verdict beside the agreement seal ---- */
.hero{display:grid;grid-template-columns:1fr auto;gap:2.5rem;align-items:center;
  margin:0 0 2rem}
.seal{position:relative;width:12rem;height:12rem;margin:0;flex:none}
.seal svg{width:100%;height:100%;transform:rotate(-90deg)}
.seal circle{fill:none;stroke-width:7}
.seal-bg{stroke:var(--rule-soft)}
.seal-fg{stroke:var(--bench);stroke-linecap:round;
  transition:stroke-dashoffset 1.6s cubic-bezier(.22,.61,.36,1)}
.seal--bad .seal-fg{stroke:var(--differs)}
.seal figcaption{position:absolute;inset:0;display:grid;place-content:center;
  text-align:center;gap:.1rem}
.seal b{font-family:var(--serif);font-size:2.15rem;font-weight:400;
  letter-spacing:-.02em;line-height:1}
.seal span{font-size:.58rem;letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);max-width:7.5rem;margin:0 auto}
.seal i{font-style:normal;font-size:.66rem;font-weight:600;color:var(--bench)}
.seal--bad i{color:var(--differs)}
@media (prefers-reduced-motion:reduce){.seal-fg{transition:none}}
/* Brass is the benchmark's colour and nothing else's. */
.btag{font-size:.56rem;letter-spacing:.13em;text-transform:uppercase;font-weight:700;
  font-family:var(--sans);color:var(--bench);border:1px solid var(--bench);
  background:var(--bench-bg);padding:.1rem .38rem;border-radius:2px;
  white-space:nowrap;vertical-align:middle;margin-left:.45rem}
/* A cell is a link to the exact tinted spot it was drawn from. */
a.loc{color:inherit;text-decoration:none}
a.loc:hover,a.loc:focus-visible{color:var(--accent)}
a.loc:hover::after,a.loc:focus-visible::after{content:" \\2197";font-size:.72em;
  color:var(--accent)}
tr.r:hover td,.prose:hover{background:var(--panel)}

/* ---- review views: side by side / tracked changes / before / after ---- */
.views{display:inline-flex;border:1px solid var(--rule);border-radius:3px;
  overflow:hidden;background:var(--paper)}
.views label{padding:.3rem .65rem;font-size:.74rem;cursor:pointer;
  color:var(--ink-soft);border-left:1px solid var(--rule-soft);display:flex;
  gap:.3rem;align-items:center;white-space:nowrap}
.views label:first-child{border-left:0}
.views input{position:absolute;opacity:0;pointer-events:none}
.views label:has(input:checked){background:var(--accent);color:var(--paper)}
.views label:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:-2px}
.tracked{display:none}
td.side.tracked{display:none}
.fig del{margin-right:.3rem}
/* View modes are section-scoped: the global switch stamps every section's
   data-view, and a section's own mini switch overrides it. */
.sec[data-view=tracked] .side-a,.sec[data-view=tracked] .side-b,
.sec[data-view=tracked] .ln-a,.sec[data-view=tracked] .ln-b{display:none}
.sec[data-view=tracked] div.side.tracked,
.sec[data-view=tracked] span.ln.tracked{display:block}
.sec[data-view=tracked] td.side.tracked{display:table-cell}
.sec[data-view=tracked] .prose{grid-template-columns:2.4rem 1fr 2.4rem}
/* Before: the benchmark as written. After: the compared document as written. */
.sec[data-view=before] .side-b,.sec[data-view=before] .gut-b,
.sec[data-view=before] .ln-b{display:none}
.sec[data-view=after] .side-a,.sec[data-view=after] .gut-a,
.sec[data-view=after] .ln-a{display:none}
.sec[data-view=before] .prose,.sec[data-view=after] .prose{
  grid-template-columns:2.4rem 1fr 2.4rem}
.sec--added[data-view=before],.sec[data-view=before] .prose--added,
.sec[data-view=before] .r--added{display:none}
.sec--removed[data-view=after],.sec[data-view=after] .prose--removed,
.sec[data-view=after] .r--removed{display:none}
.sec[data-view=before] .alt,.sec[data-view=after] .alt{display:none}
/* The per-section switch, quiet until hovered. */
.secviews{display:inline-flex;border:1px solid var(--rule-soft);border-radius:3px;
  overflow:hidden;opacity:.55}
.shead:hover .secviews,.secviews:focus-within{opacity:1}
.secviews button{font:inherit;font-size:.62rem;font-weight:600;letter-spacing:.04em;
  padding:.14rem .45rem;border:0;background:none;color:var(--muted);cursor:pointer;
  border-left:1px solid var(--rule-soft)}
.secviews button:first-child{border-left:0}
.secviews button.on{background:var(--accent);color:var(--accent-ink,#fff)}
.secviews button:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}

/* ---- review console: a decision per deviating section ---- */
.decide{display:inline-flex;gap:.3rem;align-items:center}
.dbtn{font:inherit;font-size:.64rem;font-weight:700;letter-spacing:.05em;
  text-transform:uppercase;padding:.16rem .5rem;border-radius:3px;cursor:pointer;
  border:1px solid currentColor;background:none}
.dbtn--acc{color:var(--same)} .dbtn--rej{color:var(--differs)}
.dbtn--acc:hover,.dbtn--acc.on{background:var(--add-bg)}
.dbtn--rej:hover,.dbtn--rej.on{background:var(--differs-bg)}
.dbtn.on{box-shadow:inset 0 0 0 1px currentColor}
.dstate{font-size:.64rem;font-weight:700;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted)}
.sec--decided{opacity:.72}
.sec--decided:hover,.sec--decided:focus-within{opacity:1}
body.only-open .sec--decided,body.only-open .sec:not(.sec--needs){display:none}
/* Progress: how much of the review is done, not how much matches. */
.console{position:sticky;top:0;z-index:6;background:var(--paper);
  border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);
  padding:.7rem 0 .75rem;margin:0 0 1.5rem;display:flex;gap:1.1rem;
  align-items:center;flex-wrap:wrap}
.pwrap{flex:1 1 15rem;min-width:12rem}
.ptop{display:flex;justify-content:space-between;font-size:.7rem;
  letter-spacing:.07em;text-transform:uppercase;color:var(--muted);
  margin-bottom:.32rem}
.ppct{font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink);
  font-weight:600}
.ptrack{height:5px;background:var(--rule-soft);border-radius:3px;overflow:hidden}
.pbar{height:100%;width:0;background:var(--same);border-radius:3px;
  transition:width .5s cubic-bezier(.22,.61,.36,1)}
@media (prefers-reduced-motion:reduce){.pbar{transition:none}}
.cbtn{font:inherit;font-size:.75rem;font-weight:600;padding:.36rem .7rem;
  border:1px solid var(--rule);border-radius:5px;background:var(--card,var(--paper));
  color:var(--ink-soft);cursor:pointer}
.cbtn:hover{border-color:var(--muted);color:var(--ink)}
.done-note{display:none;align-items:center;gap:.6rem;background:var(--add-bg);
  border-left:3px solid var(--same);color:var(--same);font-weight:600;
  padding:.7rem 1rem;border-radius:0 5px 5px 0;margin:0 0 1.2rem;font-size:.88rem}
body.all-done .done-note{display:flex}
.done-note b{font-size:1.15rem}

/* ---- independent reconciliation ---- */
.assure{border-top:2px solid var(--ink);padding:1rem 0 1.3rem;margin:0 0 2rem}
.assure--good{border-top-color:var(--same)}
.assure--bad{border-top-color:var(--differs)}
.ahead{display:flex;gap:1rem;align-items:baseline;flex-wrap:wrap}
.ahead h2{font-family:var(--serif);font-weight:400;font-size:1.3rem;margin:0;flex:1}
.acov{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.9rem;
  color:var(--ink-soft)}
.assure .sub{margin:.4rem 0 .7rem;max-width:74ch}
.averdict{font-family:var(--serif);font-size:1rem;margin:0 0 1rem;max-width:74ch}
.assure--good .averdict{color:var(--same)}
.assure--bad .averdict{color:var(--differs)}
.lh{font-size:.72rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:600;margin:1rem 0 .4rem}
table.ledger{border-collapse:collapse;width:100%;font-size:.84rem}
table.ledger th{font-size:.64rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);text-align:left;font-weight:600;padding:0 .8rem .35rem 0;
  border-bottom:1px solid var(--ink)}
table.ledger td{padding:.3rem .8rem .3rem 0;border-bottom:1px solid var(--rule-soft);
  vertical-align:top}
.lv{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600;
  color:var(--differs);white-space:nowrap;text-align:right}
.lc{font-family:var(--mono);font-size:.76rem;color:var(--muted);white-space:nowrap}
.lp{font-family:var(--mono);font-size:.78rem;color:var(--muted);white-space:nowrap}
.lx{font-family:var(--mono);font-size:.74rem;color:var(--ink-soft);
  max-width:44rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lmore{font-size:.78rem;color:var(--muted);font-style:italic}
.labsent{font-size:.62rem;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;color:#fff;background:var(--differs);
  padding:.08rem .35rem;border-radius:2px;white-space:nowrap}
.lcount{font-family:var(--mono);font-size:.72rem;color:var(--muted);
  white-space:nowrap}

/* ---- in-page source preview: the highlighted page, spotlighted ---- */
.peek{position:fixed;inset:0;z-index:50;display:flex;align-items:center;
  justify-content:center;background:rgba(8,12,10,.55);padding:2vh 2vw}
.peek[hidden]{display:none}
.peek-card{background:var(--paper);color:var(--ink);width:min(62rem,96vw);
  height:min(90vh,70rem);display:flex;flex-direction:column;
  border:1px solid var(--rule);border-radius:4px;
  box-shadow:0 24px 64px rgba(0,0,0,.4)}
.peek-head{display:flex;gap:1rem;align-items:center;padding:.6rem .95rem;
  border-bottom:1px solid var(--rule)}
.peek-head b{font-family:var(--serif);font-weight:400;font-size:.95rem;flex:1;
  min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.peek-head a{font-size:.78rem;white-space:nowrap}
.peek-x{background:none;border:0;font-size:1.25rem;line-height:1;cursor:pointer;
  color:var(--muted);padding:.15rem .4rem}
.peek-x:hover,.peek-x:focus-visible{color:var(--ink)}
.peek-body{overflow:auto;flex:1;background:var(--panel);padding:.8rem}
.peek-canvas{position:relative;margin:0 auto;max-width:56rem;
  box-shadow:0 2px 14px rgba(0,0,0,.18)}
.peek-canvas img{display:block;width:100%;height:auto}
.peek-box{position:absolute;border:2px solid var(--accent);border-radius:2px;
  background:rgba(14,90,86,.13);box-shadow:0 0 0 4px rgba(14,90,86,.12)}
@media (prefers-reduced-motion:no-preference){
  .peek-box{animation:peekpulse 1.5s ease-in-out 2}
}
@keyframes peekpulse{50%{box-shadow:0 0 0 10px rgba(14,90,86,.05)}}
.eyebrow{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);font-weight:600;margin:0 0 .7rem}
h1{font-family:var(--serif);font-weight:400;font-size:clamp(1.7rem,3.6vw,2.5rem);
  line-height:1.14;margin:0 0 .8rem;text-wrap:balance}
.standfirst{font-family:var(--serif);color:var(--ink-soft);max-width:62ch;
  font-size:1.05rem;margin:0 0 2rem}
.caution{border-left:3px solid var(--accent);background:var(--panel);
  padding:1rem 1.2rem;margin:0 0 2rem;max-width:72ch;font-size:.9rem;
  color:var(--ink-soft)}
.caution strong{color:var(--ink)}
.caution--marks{border-left-color:var(--same)}
.caution--copies{border-left-color:var(--differs)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));
  border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);margin:0 0 1.5rem}
.stat{padding:.9rem 1rem 1rem}
.stat+.stat{border-left:1px solid var(--rule-soft)}
.stat b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:1.5rem;
  font-weight:400;display:block;line-height:1}
.stat span{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);display:block;margin-top:.45rem}
.stat--bad b{color:var(--differs)} .stat--good b{color:var(--same)}
.bar{display:flex;flex-wrap:wrap;gap:1.2rem;align-items:center;
  border-bottom:1px solid var(--rule);padding:0 0 1rem;margin:0 0 1.8rem;
  font-size:.82rem;color:var(--muted)}
.bar label{display:flex;gap:.4rem;align-items:center;cursor:pointer;color:var(--ink-soft)}
.key{display:inline-flex;gap:.35rem;align-items:center}
.key i{width:.85rem;height:.85rem;border-radius:2px;display:inline-block}
.docs{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;margin:0 0 2rem;
  border-top:1px solid var(--rule);padding-top:1rem}
.docs div{font-size:.85rem;color:var(--ink-soft)}
.docs b{font-family:var(--serif);font-size:1.3rem;color:var(--accent);
  font-weight:400;display:block;line-height:1;margin-bottom:.35rem}
.docs code{font-family:var(--mono);font-size:.76rem;word-break:break-all}

.sec{border-top:1px solid var(--rule);margin:0 0 1.6rem;padding-top:.85rem}
.shead{display:flex;flex-wrap:wrap;gap:.6rem;align-items:baseline;margin-bottom:.6rem}
.shead h3{font-family:var(--serif);font-weight:400;font-size:1.02rem;margin:0;
  flex:1 1 20rem;min-width:0}
.serial{font-family:var(--mono);font-size:.74rem;font-weight:600;color:var(--paper);
  background:var(--ink);padding:.1rem .42rem;border-radius:2px;white-space:nowrap}
.snum{font-family:var(--mono);font-size:.74rem;font-weight:600;color:var(--paper);
  background:var(--accent);padding:.1rem .4rem;border-radius:2px;white-space:nowrap}
.pl{color:var(--accent);text-decoration:none;border-bottom:1px dotted currentColor}
.pl:hover,.pl:focus-visible{background:var(--panel)}
.gut .pl{border-bottom:0}
body.hide-unmarked .sec--unmarked{display:none}
.skind{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);border:1px solid var(--rule);padding:.12rem .38rem;border-radius:2px}
.spages{font-family:var(--mono);font-size:.72rem;color:var(--muted);white-space:nowrap}
.sdetail{font-size:.72rem;color:var(--differs)}
.chip{font-size:.62rem;letter-spacing:.08em;text-transform:uppercase;font-weight:600;
  padding:.14rem .42rem;border:1px solid currentColor;border-radius:2px;white-space:nowrap}
.chip--same{color:var(--muted)} .chip--changed{color:var(--differs)}
.chip--formatting{color:var(--accent)}
.chip--reordered{color:var(--bench)}
.chip--added{color:var(--add)} .chip--removed{color:var(--del)}
.chip--moved{color:var(--bench)}
/* Composite match score, banded the way the reviewer reads it:
   100 exact / 75-99 close / 50-74 partial / below 50 low. */
.scorechip{font-family:var(--mono);font-variant-numeric:tabular-nums;
  font-size:.72rem;font-weight:600;padding:.1rem .38rem;border-radius:2px;
  white-space:nowrap}
.t-exact{color:var(--same);background:var(--add-bg)}
.t-high{color:var(--warn);background:var(--warn-bg)}
.t-part{color:var(--differs);background:var(--differs-bg)}
.t-low{color:#fff;background:var(--differs)}
.rm{white-space:nowrap}

.scroll{overflow-x:auto}
table.rows{border-collapse:collapse;width:100%}
table.rows td{vertical-align:top;padding:.3rem .55rem;border-bottom:1px solid var(--rule-soft)}
.gut{font-family:var(--mono);font-size:.68rem;color:var(--muted);text-align:right;
  white-space:nowrap;user-select:none}
/* Two real columns, each pinned to half the width. Without a fixed layout a
   long row label grows its own cell and shoves the other document off screen —
   which defeats the entire point of a side-by-side. */
.rows--cols{table-layout:fixed}
.rows--cols .gut{width:2.2rem}
.rows--cols .figs{white-space:normal}
.rows--cols .fig{min-width:4rem}
/* The stacked layout has one content column, so it may exceed the viewport and
   scroll as a unit; both documents move together and stay aligned. */
.rows--stacked{min-width:max-content}
.rows--stacked .gut{width:2.4rem}
.side--empty{background:repeating-linear-gradient(135deg,transparent,transparent 4px,
  var(--rule) 4px,var(--rule) 5px);opacity:.5}
.label{display:block;font-size:.86rem}
.label--changed{background:var(--differs-bg);box-shadow:0 0 0 2px var(--differs-bg)}
.figs{display:block;margin-top:.15rem;white-space:nowrap}
.fig{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.78rem;
  display:inline-block;min-width:4.6rem;text-align:right;padding:0 .25rem;color:var(--ink-soft)}
.fig--changed{background:var(--differs-bg);color:var(--differs);font-weight:600;border-radius:2px}
.fig--absent{color:var(--differs);opacity:.85}
.absent{font-size:.74rem;color:var(--muted);font-style:italic}
.r--figures-differ .label,.r--columns-differ .label{color:var(--ink)}
.r--same .label{color:var(--ink-soft)}
td.stack{width:auto}
.ln{display:block;white-space:nowrap;margin-top:.1rem}
.ln em{font-family:var(--mono);font-size:.64rem;font-style:normal;color:var(--muted);
  display:inline-block;width:2.9rem;letter-spacing:.04em}
.ln--absent em{color:var(--differs)}
.alt{display:block;font-size:.8rem;color:var(--differs);margin-top:.05rem}
.wrapnote{font-size:.72rem;color:var(--muted);font-style:italic;padding:.2rem .55rem}
.r--wrap td{border-bottom:1px dashed var(--rule-soft)}
/* Which column is which, kept on screen while scrolling a 600-section page. */
.colhead{position:sticky;top:0;z-index:5;display:grid;
  grid-template-columns:2.4rem 1fr 1fr 2.4rem;gap:.55rem;
  background:var(--paper);border-bottom:2px solid var(--ink);
  padding:.5rem 0 .45rem;margin-bottom:.2rem}
.ch{font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;font-weight:600;
  color:var(--accent);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ch i{font-style:normal;font-weight:400;letter-spacing:.02em;text-transform:none;
  color:var(--muted);font-size:.68rem}
.ch i:before{content:" · "}
.cgut{font-size:.6rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  text-align:right}
.ch-mode{display:none;font-size:.7rem;letter-spacing:.06em;text-transform:none;
  font-weight:600;color:var(--accent);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
body.view-tracked .ch-a,body.view-tracked .ch-b,
body.view-before .ch-b,body.view-after .ch-a{display:none}
body.view-tracked .ch-mode{display:block}
body.view-tracked .colhead,body.view-before .colhead,body.view-after .colhead{
  grid-template-columns:2.4rem 1fr 2.4rem}
/* Two real columns when the table is narrow enough for them to fit. */
.rows--cols .side+.side{border-left:1px solid var(--rule)}

.prose{display:grid;grid-template-columns:2.4rem 1fr 1fr 2.4rem;gap:.55rem;
  border-bottom:1px solid var(--rule-soft);padding:.3rem 0}
.prose .side{width:auto;min-width:0}
.para{margin:0;font-size:.87rem;max-width:68ch}
/* Every differing word carries a colour of its own, so what to look at is
   never a matter of squinting. A formatting-only difference used to be a
   dotted grey underline, which on a 99% row was invisible — and "99% and I
   cannot see why" is the one thing a reviewer cannot work with. */
del{background:var(--del-bg);color:var(--del);text-decoration:line-through;
  border-radius:2px;padding:0 .12rem;font-weight:600}
ins{background:var(--add-bg);color:var(--add);text-decoration:none;
  border-radius:2px;padding:0 .12rem;font-weight:600}
u.fmt{text-decoration:none;background:var(--warn-bg);color:var(--warn);
  border-bottom:1px dotted var(--warn);border-radius:2px;padding:0 .12rem}
/* Words that only changed place. Every one is present on both sides. */
.mv{background:var(--bench-bg);border-bottom:1px dashed var(--bench);
  color:var(--bench);border-radius:2px;padding:0 .12rem}

/* ---- executive verdict ---- */
.verdict{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));
  border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);margin:0 0 2rem}
.v{padding:1.15rem 1.25rem 1.3rem}
.v+.v{border-left:1px solid var(--rule-soft)}
.v b{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:2rem;
  font-weight:400;line-height:1;display:block;letter-spacing:-.02em}
.v b em{font-style:normal;font-size:1.1rem;color:var(--muted)}
.v span{display:block;margin-top:.5rem;font-size:.74rem;letter-spacing:.08em;
  text-transform:uppercase;color:var(--ink-soft);font-weight:600}
.v i{display:block;margin-top:.2rem;font-style:normal;font-size:.76rem;color:var(--muted)}
.v--good b{color:var(--same)} .v--bad b{color:var(--differs)}
/* ---- section register ---- */
.register{margin:0 0 2.5rem}
.register h2{font-family:var(--serif);font-weight:400;font-size:1.35rem;margin:0 0 .25rem}
.register table{border-collapse:collapse;width:100%;font-size:.86rem}
.register thead th{font-size:.66rem;letter-spacing:.09em;text-transform:uppercase;
  color:var(--muted);font-weight:600;text-align:left;padding:0 .8rem .45rem 0;
  border-bottom:1px solid var(--ink);white-space:nowrap}
.register td{padding:.42rem .8rem .42rem 0;border-bottom:1px solid var(--rule-soft);
  vertical-align:top}
.reg--changed{background:linear-gradient(90deg,var(--differs-bg),transparent 42%)}
.reg--formatting .rn{background:var(--accent)}
.rn{font-family:var(--mono);font-size:.74rem;color:var(--paper);width:1.9rem}
.reg .rn{background:var(--ink);text-align:center;border-radius:2px;padding:.1rem 0;
  height:1.1rem;line-height:1.1rem}
.rt{max-width:32rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rp{font-family:var(--mono);font-size:.76rem;white-space:nowrap;color:var(--muted)}
.rd{font-family:var(--mono);font-size:.74rem;color:var(--differs);white-space:nowrap}
.rl a{font-size:.74rem;color:var(--accent);text-decoration:none;
  border-bottom:1px dotted currentColor}
body.hide-same .sec--same{display:none}
body.hide-oneside .sec--added,body.hide-oneside .sec--removed{display:none}
.toc{border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);
  margin:0 0 2rem;padding:.9rem 0 1rem}
.toc h2{font-family:var(--serif);font-weight:400;font-size:1rem;margin:0 0 .6rem}
.toc ol{margin:0;padding:0;list-style:none;display:grid;gap:.28rem;
  grid-template-columns:repeat(auto-fill,minmax(24rem,1fr))}
.toc a{color:var(--ink-soft);text-decoration:none;font-size:.82rem;display:flex;gap:.5rem}
.toc a:hover,.toc a:focus-visible{color:var(--accent);text-decoration:underline}
.toc .s{font-family:var(--mono);font-size:.7rem;color:var(--paper);
  background:var(--ink);border-radius:2px;padding:0 .3rem;min-width:1.6rem;
  text-align:center;flex:none}
.toc .n{font-family:var(--mono);font-size:.7rem;color:var(--differs);
  min-width:2.6rem;text-align:right;flex:none}
.toc .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
summary:focus-visible,a:focus-visible,input:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
html{scroll-behavior:smooth}
@media (max-width:60rem){
  .docs,.prose{grid-template-columns:1fr}
  .prose .gut{display:none}
  .hero{grid-template-columns:1fr}
  .seal{margin:0 auto}
}
footer{border-top:1px solid var(--rule);margin-top:2.5rem;padding-top:1rem;
  color:var(--muted);font-size:.8rem;max-width:72ch}
"""

_JS = """
for (const [id, cls] of [['hide-same', 'hide-same'], ['hide-oneside', 'hide-oneside'],
                         ['hide-unmarked', 'hide-unmarked']]) {
  const box = document.getElementById(id);
  if (box) box.addEventListener('change', function (e) {
    document.body.classList.toggle(cls, e.target.checked);
  });
}
// Draw the agreement seal up to its measured value. With reduced motion the
// transition is off in CSS and the ring simply appears complete.
const seal = document.querySelector('.seal-fg');
if (seal) requestAnimationFrame(() => requestAnimationFrame(() => {
  seal.style.strokeDashoffset = seal.dataset.target;
}));

// Review views: side by side, tracked changes (Word-style redline against the
// benchmark), before (the benchmark as written), after (the compared document).
// The global switch sets every section; a section's own switch overrides it,
// because a reviewer reads most of a report one way and one passage another.
const MODE_NOTE = {
  tracked: 'Tracked changes \\u2014 struck through: in the benchmark, not carried over \\u00b7 underlaid green: added, not in the benchmark',
  before: '', after: ''
};
const SECTIONS = Array.from(document.querySelectorAll('.sec'));
function setSectionView(sec, view) {
  if (view === 'sbs') sec.removeAttribute('data-view');
  else sec.setAttribute('data-view', view);
  for (const b of sec.querySelectorAll('.secviews button'))
    b.classList.toggle('on', b.dataset.sview === view);
}
for (const radio of document.querySelectorAll('input[name="view"]')) {
  radio.addEventListener('change', () => {
    for (const sec of SECTIONS) setSectionView(sec, radio.value);
    const note = document.querySelector('.ch-mode');
    if (note) note.textContent = MODE_NOTE[radio.value] || '';
    document.body.classList.toggle('view-tracked', radio.value === 'tracked');
    document.body.classList.toggle('view-before', radio.value === 'before');
    document.body.classList.toggle('view-after', radio.value === 'after');
  });
}
for (const sec of SECTIONS) setSectionView(sec, 'sbs');
document.addEventListener('click', e => {
  const b = e.target.closest('.secviews button');
  if (!b) return;
  setSectionView(b.closest('.sec'), b.dataset.sview);
});

// Accept / reject, saved in this browser. A reviewer working through a long
// statement over two sittings should not lose the first one; the key includes
// the two documents and the serial count, so a regenerated report with
// different content starts clean rather than showing stale decisions.
const DKEY = 'fincheck:' + document.title + ':' + SECTIONS.length;
let decisions = {};
try { decisions = JSON.parse(localStorage.getItem(DKEY) || '{}') || {}; } catch (e) {}
const NEEDED = SECTIONS.filter(s => s.classList.contains('sec--needs'));

function paintDecision(sec) {
  const serial = sec.dataset.serial;
  const verdict = decisions[serial];
  const box = sec.querySelector('.decide');
  if (box) {
    for (const b of box.querySelectorAll('.dbtn'))
      b.classList.toggle('on', b.dataset.decide === verdict);
    const state = box.querySelector('.dstate');
    if (state) state.textContent = verdict ? verdict : '';
  }
  sec.classList.toggle('sec--decided', !!verdict);
}
function paintProgress() {
  const done = NEEDED.filter(s => decisions[s.dataset.serial]).length;
  const pct = NEEDED.length ? Math.round(100 * done / NEEDED.length) : 100;
  const bar = document.getElementById('pbar');
  const label = document.getElementById('ppct');
  const count = document.getElementById('pcount');
  if (bar) bar.style.width = pct + '%';
  if (label) label.textContent = pct + '%';
  if (count) count.textContent = done + ' of ' + NEEDED.length;
  document.body.classList.toggle('all-done', NEEDED.length > 0 && done === NEEDED.length);
}
function save() {
  try { localStorage.setItem(DKEY, JSON.stringify(decisions)); } catch (e) {}
}
document.addEventListener('click', e => {
  const b = e.target.closest('.dbtn');
  if (!b) return;
  const sec = b.closest('.sec');
  const serial = sec.dataset.serial;
  decisions[serial] = decisions[serial] === b.dataset.decide ? undefined : b.dataset.decide;
  if (!decisions[serial]) delete decisions[serial];
  save(); paintDecision(sec); paintProgress();
});
for (const sec of SECTIONS) paintDecision(sec);
paintProgress();

const openOnly = document.getElementById('only-open');
if (openOnly) openOnly.addEventListener('change', e => {
  document.body.classList.toggle('only-open', e.target.checked);
});

// Export the decisions, so a review can leave the browser it was made in.
function download(name, text, mime) {
  const blob = new Blob([text], {type: mime});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function record() {
  return SECTIONS.map(s => ({
    serial: s.dataset.serial,
    section: (s.querySelector('h3') || {}).textContent || '',
    match: Number(s.dataset.score),
    status: (s.querySelector('.chip') || {}).textContent || '',
    decision: decisions[s.dataset.serial] || (s.classList.contains('sec--needs') ? 'open' : 'n/a'),
  }));
}
const csvBtn = document.getElementById('exp-csv');
if (csvBtn) csvBtn.addEventListener('click', () => {
  const q = v => '"' + String(v).replace(/"/g, '""') + '"';
  const rows = [['Serial', 'Section', 'Match %', 'Status', 'Decision']];
  for (const r of record()) rows.push([r.serial, r.section, r.match, r.status, r.decision]);
  download('review_decisions.csv', rows.map(r => r.map(q).join(',')).join('\\r\\n'),
           'text/csv');
});
const jsonBtn = document.getElementById('exp-json');
if (jsonBtn) jsonBtn.addEventListener('click', () => {
  download('review_decisions.json', JSON.stringify({
    report: document.title, exported: new Date().toISOString(), sections: record(),
  }, null, 2), 'application/json');
});
const resetBtn = document.getElementById('exp-reset');
if (resetBtn) resetBtn.addEventListener('click', () => {
  if (!Object.keys(decisions).length) return;
  if (!confirm('Clear every accept/reject decision in this report?')) return;
  decisions = {}; save();
  for (const sec of SECTIONS) paintDecision(sec);
  paintProgress();
});

// In-page source preview. External PDF viewers cannot be relied on to honour
// a link's page-and-position, so clicking a cell shows the rendered page of
// the marked copy right here, with the passage spotlighted. The PDF link is
// still offered inside the panel.
const PEEK = (() => {
  const el = document.getElementById('previews');
  if (!el) return null;
  try { return JSON.parse(el.textContent); } catch (err) { return null; }
})();
let peekEl = null, peekReturn = null;
function closePeek() {
  if (peekEl) peekEl.hidden = true;
  if (peekReturn) { peekReturn.focus(); peekReturn = null; }
}
function buildPeek() {
  peekEl = document.createElement('div');
  peekEl.className = 'peek';
  peekEl.hidden = true;
  peekEl.innerHTML =
    '<div class="peek-card" role="dialog" aria-modal="true" aria-label="Highlighted source">' +
    '<div class="peek-head"><b id="peek-title"></b>' +
    '<a id="peek-open" class="pl" target="_blank" rel="noopener">Open the PDF here</a>' +
    '<button class="peek-x" aria-label="Close" title="Close (Esc)">&#215;</button></div>' +
    '<div class="peek-body"><div class="peek-canvas">' +
    '<img id="peek-img" alt="Rendered page of the marked-up PDF">' +
    '<div class="peek-box" id="peek-box"></div></div></div></div>';
  document.body.appendChild(peekEl);
  peekEl.addEventListener('click', e => { if (e.target === peekEl) closePeek(); });
  peekEl.querySelector('.peek-x').addEventListener('click', closePeek);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePeek(); });
}
document.addEventListener('click', e => {
  const link = e.target.closest('a[data-peek]');
  if (!link || !PEEK) return;
  const [side, page, rectStr] = link.dataset.peek.split(':');
  const pv = PEEK[side] && PEEK[side][page];
  if (!pv) return;
  e.preventDefault();
  if (!peekEl) buildPeek();
  peekReturn = link;
  const rect = rectStr.split(',').map(Number);
  const name = (PEEK.labels[side] || '') + (side === 'a' ? ' (benchmark)' : '');
  peekEl.querySelector('#peek-title').textContent = name + ' \\u00b7 page ' + page;
  peekEl.querySelector('#peek-open').href = link.href;
  const img = peekEl.querySelector('#peek-img');
  const box = peekEl.querySelector('#peek-box');
  const pad = 5;
  box.style.left = (Math.max(0, rect[0] - pad) / pv.w * 100) + '%';
  box.style.top = (Math.max(0, rect[1] - pad) / pv.h * 100) + '%';
  box.style.width = (Math.min(pv.w, rect[2] - rect[0] + 2 * pad) / pv.w * 100) + '%';
  box.style.height = (Math.min(pv.h, rect[3] - rect[1] + 2 * pad) / pv.h * 100) + '%';
  const bringIntoView = () => {
    const body = peekEl.querySelector('.peek-body');
    body.scrollTop = Math.max(0, box.offsetTop - 120);
  };
  peekEl.hidden = false;
  if (img.dataset.shown === side + page) { bringIntoView(); }
  else {
    img.dataset.shown = side + page;
    img.onload = bringIntoView;
    img.src = pv.src;
  }
  peekEl.querySelector('.peek-x').focus();
}, true);
"""


def write_side_by_side(
    sections: list[Section], meta: Meta, output_path: str
) -> str:
    s = meta.summary
    tags = meta.tags
    identical_all = sum(1 for x in sections if x.status == "same")
    identical_sections = identical_all
    one_sided = sum(1 for x in sections if x.status in ("added", "removed"))

    body = "".join(_section_html(x, i, tags) for i, x in enumerate(sections))

    numbered_sections = [x for x in sections if x.marked is not None]
    numbered = bool(meta.marked_sections)
    total_sections = len(numbered_sections) or len(sections)
    identical_sections_count = sum(
        1 for x in (numbered_sections or sections) if x.status == "same"
    )
    figure_state = "bad" if s.changed_figures else "good"
    text_state = "bad" if s.changed else "good"

    benchmark = _e(meta.label_a)
    if s.changed_figures:
        headline = (
            f"{s.changed_figures:,} figure{'' if s.changed_figures == 1 else 's'} "
            "deviate from the benchmark"
        )
        standfirst = (
            f"{benchmark} is the benchmark; every figure below was matched to its "
            "counterpart and measured against it. The deviations are indexed first; "
            "each opens the exact highlighted passage it was drawn from."
        )
    elif s.changed:
        headline = "Every figure matches the benchmark; wording differs in places"
        standfirst = (
            f"{benchmark} is the benchmark. All {s.rows_matched:,} compared table "
            f"rows carry its values exactly. {s.changed:,} passage(s) deviate in "
            "wording"
            + (
                f", and {s.formatting:,} differ only in punctuation or spacing."
                if s.formatting
                else "."
            )
        )
    elif s.formatting:
        headline = "Faithful to the benchmark; only the typesetting differs"
        standfirst = (
            f"{benchmark} is the benchmark. All {s.matched:,} compared passages, "
            f"including {s.rows_matched:,} table rows, carry its content. "
            f"{s.formatting:,} differ in punctuation, spacing or bullet style alone."
        )
    else:
        headline = "Faithful to the benchmark throughout"
        standfirst = (
            f"{benchmark} is the benchmark. All {s.matched:,} compared passages, "
            f"including {s.rows_matched:,} table rows, carry its content exactly."
        )

    # The agreement seal shows the composite match: every segment's 0-100
    # score, weighted by how much content it carries, one-sided segments
    # counting as zero. One number, and the ring is drawn to it.
    pct = float(s.overall)
    pct_text = f"{s.overall}%"
    circumference = 351.86  # 2 * pi * r, r = 56
    seal_target = circumference * (1 - pct / 100.0)
    seal_state = " seal--bad" if s.changed_figures else ""
    fig_note = (
        "no figure deviations"
        if not s.changed_figures
        else f"{s.changed_figures:,} figure deviation{'' if s.changed_figures == 1 else 's'}"
    )
    seal = (
        f'<figure class="seal{seal_state}" role="img" '
        f'aria-label="composite match with the benchmark {pct_text}; {fig_note}">'
        f'<svg viewBox="0 0 132 132" aria-hidden="true">'
        f'<circle class="seal-bg" cx="66" cy="66" r="56"></circle>'
        f'<circle class="seal-fg" cx="66" cy="66" r="56" '
        f'stroke-dasharray="{circumference}" stroke-dashoffset="{circumference}" '
        f'data-target="{seal_target:.1f}"></circle>'
        f"</svg>"
        f"<figcaption><b>{pct_text}</b>"
        f"<span>composite match with benchmark</span>"
        f"<i>{fig_note}</i></figcaption>"
        f"</figure>"
    )

    def _pill(status: str) -> str:
        word = {"same": "agrees", "formatting": "formatting only",
                "reordered": "reordered",
                "changed": "deviates", "added": "not in benchmark",
                "removed": "benchmark only"}.get(status, status)
        return f'<span class="chip chip--{status}">{word}</span>'

    def _register_row(x) -> str:
        spot_a = _section_spot(x, "a", meta.heights_a)
        spot_b = _section_spot(x, "b", meta.heights_b)
        flags = _pill(x.status)
        if x.moved:
            flags += ' <span class="chip chip--moved">moved</span>'
        return (
            f'<tr class="reg reg--{x.status}">'
            f'<td class="rn">{x.serial}</td>'
            f'<td class="rt">{_e(x.title) or "&nbsp;"}</td>'
            f'<td class="rp">{_page_link(x.pages[0], meta.marked_href_a, top=spot_a[1] if spot_a else None, peek=_section_peek(x, "a"))}</td>'
            f'<td class="rp">{_page_link(x.pages[1], meta.marked_href_b, top=spot_b[1] if spot_b else None, peek=_section_peek(x, "b"))}</td>'
            f'<td class="rm">{_score_chip(x.score)}</td>'
            f'<td class="rs">{flags}</td>'
            f'<td class="rd">{f"{x.changed} of {len(x.pairs)}" if x.status == "changed" else ""}</td>'
            f'<td class="rl"><a href="#s{sections.index(x)}">view</a></td>'
            f"</tr>"
        )

    register_rows = "".join(_register_row(x) for x in (numbered_sections or sections))
    register = (
        '<section class="register"><h2>Section register</h2>'
        '<p class="sub">Every section compared, in document order. '
        "Page numbers open the marked-up copy at the exact passage.</p>"
        '<div class="scroll"><table>'
        "<thead><tr><th>#</th><th>Section</th>"
        f'<th>{_e(meta.label_a)}<span class="btag">benchmark</span></th>'
        f"<th>{_e(meta.label_b)}</th>"
        "<th>Match</th><th>Status</th><th>Deviations</th><th></th></tr></thead>"
        f"<tbody>{register_rows}</tbody></table></div></section>"
    )

    numbered_list = [x for x in sections if x.marked is not None]
    unmarked_count = len(sections) - len(numbered_list)
    blanks_in_numbered = sum(
        1 for x in numbered_list for p in x.pairs if p.a is None or p.b is None
    )
    copies_note = ""
    if meta.marked_href_a or meta.marked_href_b:
        copies_note = (
            '<div class="caution caution--copies">'
            "<strong>Every point here is numbered and located.</strong> The number "
            "on each section is stamped onto the same passage in a copy of each "
            "source PDF — "
            f'<a class="pl" href="{_e(meta.marked_href_a)}" target="_blank" '
            f'rel="noopener">{_e(meta.marked_href_a)}</a> and '
            f'<a class="pl" href="{_e(meta.marked_href_b)}" target="_blank" '
            f'rel="noopener">{_e(meta.marked_href_b)}</a>. '
            "Every cell and page number throughout opens its copy scrolled to "
            "the exact highlighted passage it was drawn from, and each copy "
            "carries the same numbers in its bookmarks. "
            f"Everything compared is tinted there — {meta.coverage_a[0]:,} "
            f"passages and {meta.coverage_a[1]:,} figures on the benchmark, "
            f"{meta.coverage_b[0]:,} and {meta.coverage_b[1]:,} on the other — "
            "so anything left untinted was <em>not</em> covered by this report "
            "and can be checked rather than assumed."
            "</div>"
        )

    marks_note = ""
    unmarked_filter = ""
    if numbered_list:
        marks_note = (
            '<div class="caution caution--marks">'
            f"<strong>Using your section numbers.</strong> {len(numbered_list)} numbered "
            "section(s) were read from the highlight comments in both files. Content "
            "you numbered <em>n</em> is compared only against content numbered "
            "<em>n</em>, so a section you marked in both documents cannot come out "
            "against a blank"
            + (
                "."
                if not blanks_in_numbered
                else f" — {blanks_in_numbered} still did, which means the two sides "
                "hold different amounts of content there."
            )
            + " Everything outside your marks falls back to content matching."
            "</div>"
        )
        unmarked_filter = (
            f'<label><input type="checkbox" id="hide-unmarked"> '
            f"Hide {unmarked_count:,} outside your marks</label>"
        )

    # An index of the sections that differ on both sides — the ones a reviewer
    # has to look at. 700 sections is too many to scroll hunting for them.
    differing = [(i, x) for i, x in enumerate(sections) if x.status == "changed"]
    toc_items = "".join(
        f'<li><a href="#s{i}"><span class="s">{x.serial}</span>'
        f'<span class="n">{x.changed}/{len(x.pairs)}</span>'
        f'<span class="t">{_e(x.title) or "&nbsp;"}</span></a></li>'
        for i, x in differing
    )
    toc = (
        f'<nav class="toc"><h2>{len(differing)} section'
        f'{"" if len(differing) == 1 else "s"} deviate from the benchmark</h2>'
        f"<ol>{toc_items}</ol></nav>"
        if differing
        else ""
    )

    ledger_panel = _ledger_panel(meta.ledger, meta.label_a, meta.label_b)
    needs_decision = sum(
        1 for x in sections if x.status in ("changed", "added", "removed")
    )

    previews_json = ""
    if meta.previews_a or meta.previews_b:
        import json

        payload = json.dumps(
            {
                "labels": {"a": meta.label_a, "b": meta.label_b},
                "a": meta.previews_a,
                "b": meta.previews_b,
            },
            separators=(",", ":"),
        ).replace("</", "<\\/")
        previews_json = (
            f'<script id="previews" type="application/json">{payload}</script>\n'
        )

    page = f"""<meta charset="utf-8">
<title>Benchmark comparison — {_e(meta.pdf_b.rsplit('/', 1)[-1])} against {_e(meta.pdf_a.rsplit('/', 1)[-1])}</title>
<style>{_CSS}</style>
<div class="wrap">
  <div class="hero">
    <div class="hero-lead">
      <p class="eyebrow">Verification &middot; {_e(meta.label_b)} against benchmark {_e(meta.label_a)}</p>
      <h1>{headline}</h1>
      <p class="standfirst">{standfirst}</p>
    </div>
    {seal}
  </div>

  <div class="verdict">
    <div class="v v--{figure_state}">
      <b>{s.changed_figures:,}</b>
      <span>figure deviation{"" if s.changed_figures == 1 else "s"}</span>
      <i>across {s.rows_matched:,} compared table rows</i>
    </div>
    <div class="v">
      <b>{identical_sections:,}<em>/{total_sections:,}</em></b>
      <span>sections match exactly</span>
      <i>matched by {"your numbering" if numbered else "heading"}</i>
    </div>
    <div class="v v--{text_state}">
      <b>{s.changed:,}</b>
      <span>passages deviating</span>
      <i>of {s.matched:,} compared{f"; {s.formatting:,} formatting only" if s.formatting else ""}</i>
    </div>
    <div class="v">
      <b>{s.only_in_a + s.only_in_b:,}</b>
      <span>present on one side</span>
      <i>{s.only_in_a:,} benchmark only &middot; {s.only_in_b:,} not in benchmark{f" &middot; {s.moved:,} moved" if s.moved else ""}</i>
    </div>
  </div>

  {register}

  <div class="caution">
    <strong>This view infers structure.</strong> A PDF holds glyphs at coordinates, not
    paragraphs and tables, so the blocks below are reconstructed from baselines, gaps and
    column positions, and the pairing is a similarity judgement. Read a difference as
    &ldquo;look at this&rdquo;, not as proof. For proof, use the exact comparison, which
    compares bytes, glyphs and pixels and infers nothing.
  </div>

  <div class="docs">
    <div><b>{_e(meta.label_a)}<span class="btag">benchmark</span></b>
      <code>{_e(meta.pdf_a.rsplit('/', 1)[-1])}</code><br>
      {meta.pages_a} pages{f" &middot; {_e(meta.producer_a)}" if meta.producer_a else ""}</div>
    <div><b>{_e(meta.label_b)}</b>
      <code>{_e(meta.pdf_b.rsplit('/', 1)[-1])}</code><br>
      {meta.pages_b} pages{f" &middot; {_e(meta.producer_b)}" if meta.producer_b else ""}</div>
  </div>

  <div class="stats">
    <div class="stat"><b>{s.matched:,}</b><span>passages matched</span></div>
    <div class="stat stat--bad"><b>{s.changed:,}</b><span>matched, deviating</span></div>
    <div class="stat stat--bad"><b>{s.changed_figures:,}</b><span>figure cells deviating</span></div>
    <div class="stat stat--good"><b>{s.unchanged:,}</b><span>matched and agreeing</span></div>
    <div class="stat"><b>{s.only_in_a:,}</b><span>benchmark only</span></div>
    <div class="stat"><b>{s.only_in_b:,}</b><span>not in benchmark</span></div>
  </div>

  {marks_note}
  {copies_note}
  {ledger_panel}

  <div class="done-note"><b>&#10003;</b>
    <span>Every deviation has been reviewed. Export the decisions to keep the
    record with the file.</span></div>

  <div class="console">
    <div class="pwrap">
      <div class="ptop"><span>Review decisions</span>
        <span><span id="pcount">0 of {needs_decision:,}</span>
        &middot; <span class="ppct" id="ppct">0%</span></span></div>
      <div class="ptrack"><div class="pbar" id="pbar"></div></div>
    </div>
    <label><input type="checkbox" id="only-open"> Only undecided</label>
    <button class="cbtn" id="exp-csv" type="button">Export CSV</button>
    <button class="cbtn" id="exp-json" type="button">Export JSON</button>
    <button class="cbtn" id="exp-reset" type="button">Reset</button>
  </div>

  <div class="bar">
    <span class="views" role="radiogroup" aria-label="Review view">
      <label><input type="radio" name="view" value="sbs" checked> Side by side</label>
      <label><input type="radio" name="view" value="tracked"> Tracked changes</label>
      <label><input type="radio" name="view" value="before"> Before &middot; benchmark</label>
      <label><input type="radio" name="view" value="after"> After &middot; compared</label>
    </span>
    <label><input type="checkbox" id="hide-same"> Hide {identical_sections:,} identical</label>
    <label><input type="checkbox" id="hide-oneside"> Hide {one_sided:,} one-sided</label>
    {unmarked_filter}
    <span class="key"><i style="background:var(--differs-bg)"></i> figure deviates</span>
    <span class="key"><i style="background:var(--del-bg)"></i> in benchmark only</span>
    <span class="key"><i style="background:var(--add-bg)"></i> not in benchmark</span>
    <span class="key"><i style="background:var(--warn-bg)"></i> punctuation/spacing</span>
    <span class="key"><i style="background:var(--bench-bg)"></i> moved</span>
    <span>{s.paragraphs_matched:,} paragraphs &middot; {s.rows_matched:,} table rows</span>
  </div>

{toc}
  <div class="colhead">
    <span class="cgut">pg</span>
    <span class="ch ch-a">{_e(meta.label_a)}<span class="btag">benchmark</span><i>{_e(meta.made_a)}</i></span>
    <span class="ch ch-b">{_e(meta.label_b)}<i>{_e(meta.made_b)}</i></span>
    <span class="ch-mode"></span>
    <span class="cgut">pg</span>
  </div>
{body}

  <footer>
    Paragraphs are compared whole, because the two files wrap lines at different
    measures; table rows are compared individually, because the files group rows into
    tables differently. Matching uses exact-content anchors first, then a similarity
    alignment that preserves document order. Figures shown as extracted; the filings use
    Indian digit grouping.
  </footer>
</div>
{previews_json}<script>{_JS}</script>
"""
    # UTF-8 explicitly: a filing is full of ₹, €, en-dashes and curly quotes,
    # and on Windows the default encoding is cp1252, which cannot represent
    # them. Writing the report crashed on the rupee sign.
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return output_path
