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
from dataclasses import dataclass
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

    @property
    def tags(self) -> "Tags":
        return Tags(
            short_a=_short_tag(self.label_a),
            short_b=_short_tag(self.label_b),
            full_a=self.label_a,
            full_b=self.label_b,
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


# How a PDF was made is usually the most useful thing to call it: "the Excel
# one" and "the HTML one" is how people actually refer to these two files.
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


def default_label(path: str, producer: str, creator: str = "") -> str:
    """Name a document by how it was produced, falling back to its filename."""
    haystack = f"{producer} {creator}".lower()
    for needle, label in _PRODUCERS:
        if needle in haystack:
            return label
    stem = path.rsplit("/", 1)[-1]
    return stem[:-4] if stem.lower().endswith(".pdf") else stem




def _e(text) -> str:
    return html.escape(str(text))


def _figure_cells(values, changed: set, missing: int = 0) -> str:
    cells = "".join(
        f'<span class="fig{" fig--changed" if i in changed else ""}">'
        f"{_e(format_number(v))}</span>"
        for i, v in enumerate(values)
    )
    cells += '<span class="fig fig--absent">—</span>' * missing
    return cells


# Beyond this many figure columns, two side-by-side copies cannot both fit on
# screen, so A is stacked over B instead. Reading down a column beats scrolling
# sideways to find its counterpart.
WIDE_TABLE_COLUMNS = 6


def _stacked_row_html(pair: Pair, tags: Tags) -> str:
    """One row with the two documents stacked, columns aligned, for a wide table."""
    changed = {i for i, _, _ in pair.changed_figures}
    a, b = pair.a, pair.b
    status = pair.status

    def line(unit, other, tag: str) -> str:
        if unit is None:
            return (
                f'<span class="ln ln--absent"><em>{tag}</em>'
                f'<span class="absent">not present</span></span>'
            )
        gap = 0
        if other is not None and len(unit.values) < len(other.values):
            gap = len(other.values) - len(unit.values)
        return (
            f'<span class="ln"><em>{tag}</em>'
            f'<span class="figs">{_figure_cells(unit.values, changed, gap)}</span>'
            f"</span>"
        )

    label = (a or b).text
    label_cls = "label label--changed" if status == "label-differs" else "label"
    alt = ""
    if status == "label-differs" and a is not None and b is not None:
        alt = f'<span class="alt">{_e(tags.full_b)}: {_e(b.text)}</span>'

    return (
        f'<tr class="r r--{status}">'
        f'<td class="gut">{_e(a.page) if a else ""}</td>'
        f'<td class="stack">'
        f'<span class="{label_cls}">{_e(label) or "&nbsp;"}</span>{alt}'
        f"{line(a, b, tags.short_a)}{line(b, a, tags.short_b)}"
        f"</td>"
        f'<td class="gut">{_e(b.page) if b else ""}</td>'
        f"</tr>"
    )


def _row_pair_html(pair: Pair, tags: Tags) -> str:
    changed = {i for i, _, _ in pair.changed_figures}
    status = pair.status
    a, b = pair.a, pair.b

    def side(unit, other, is_a: bool) -> str:
        if unit is None:
            return '<td class="side side--empty"><span class="absent">not present</span></td>'
        label_cls = "label"
        if status == "label-differs":
            label_cls += " label--changed"
        gap = 0
        if other is not None and len(unit.values) < len(other.values):
            gap = len(other.values) - len(unit.values)
        return (
            f'<td class="side">'
            f'<span class="{label_cls}">{_e(unit.text) or "&nbsp;"}</span>'
            f'<span class="figs">{_figure_cells(unit.values, changed if not is_a else changed, gap)}</span>'
            f"</td>"
        )

    return (
        f'<tr class="r r--{status}">'
        f'<td class="gut">{_e(a.page) if a else ""}</td>'
        f"{side(a, b, True)}"
        f"{side(b, a, False)}"
        f'<td class="gut">{_e(b.page) if b else ""}</td>'
        f"</tr>"
    )


def _paragraph_html(pair: Pair) -> str:
    a, b = pair.a, pair.b
    if a is not None and b is not None and not pair.words:
        # A table row that landed in a prose section: no word diff was computed,
        # so show both sides plainly rather than rendering two empty paragraphs.
        return (
            f'<div class="prose prose--{pair.status}">'
            f'<div class="gut">{_e(a.page)}</div>'
            f'<div class="side"><p class="para">{_e(a.text)}'
            f'<span class="figs">{_figure_cells(a.values, set())}</span></p></div>'
            f'<div class="side"><p class="para">{_e(b.text)}'
            f'<span class="figs">{_figure_cells(b.values, set())}</span></p></div>'
            f'<div class="gut">{_e(b.page)}</div></div>'
        )
    if a is None or b is None:
        only = a or b
        text = f'<p class="para">{_e(only.text)}</p>'
        empty = '<div class="side side--empty"><span class="absent">not present</span></div>'
        left = f'<div class="side">{text}</div>' if a is not None else empty
        right = f'<div class="side">{text}</div>' if b is not None else empty
        return (
            f'<div class="prose prose--{pair.status}">'
            f'<div class="gut">{_e(a.page) if a else ""}</div>{left}{right}'
            f'<div class="gut">{_e(b.page) if b else ""}</div></div>'
        )

    left_parts, right_parts = [], []
    for op, text in pair.words:
        if not text:
            continue
        if op == "=":
            left_parts.append(_e(text))
            right_parts.append(_e(text))
        elif op == "-":
            left_parts.append(f"<del>{_e(text)}</del>")
        else:
            right_parts.append(f"<ins>{_e(text)}</ins>")

    return (
        f'<div class="prose prose--{pair.status}">'
        f'<div class="gut">{_e(a.page)}</div>'
        f'<div class="side"><p class="para">{" ".join(left_parts)}</p></div>'
        f'<div class="side"><p class="para">{" ".join(right_parts)}</p></div>'
        f'<div class="gut">{_e(b.page)}</div>'
        f"</div>"
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
                f'<td class="wrapnote" colspan="2">{run} wrapped label line'
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
    "same": "identical",
    "changed": "differs",
    "added": "only in B",
    "removed": "only in A",
}


def _section_html(section: Section, index: int, tags: Tags) -> str:
    pages_a, pages_b = section.pages
    status = section.status
    label = _STATUS_LABEL.get(status, status)
    kind = "Table" if section.kind == "table" else "Text"
    # Only meaningful where both sides exist; on a one-sided section every pair
    # counts as changed, and "1 of 1 differ" would just restate the chip.
    detail = (
        f"{section.changed} of {len(section.pairs)} differ"
        if status == "changed"
        else ""
    )

    head = (
        f'<div class="shead">'
        f'<span class="skind">{kind}</span>'
        f'<h3>{_e(section.title) or "&nbsp;"}</h3>'
        f'<span class="spages">A&nbsp;{_e(pages_a)} &middot; B&nbsp;{_e(pages_b)}</span>'
        f'<span class="chip chip--{status}">{label}</span>'
        f'{f"<span class=sdetail>{detail}</span>" if detail else ""}'
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
        body = "".join(_paragraph_html(p) for p in section.pairs)

    return f'<section class="sec sec--{status}" id="s{index}">{head}{body}</section>'


_CSS = """
:root {
  --paper:#FBFAF7; --panel:#F3F2ED; --ink:#16191A; --ink-soft:#4A514D;
  --muted:#6C736E; --rule:#C8CEC9; --rule-soft:#E4E7E2; --accent:#0E5A56;
  --differs:#9B3220; --differs-bg:#F6E3DE; --same:#2C6A4E;
  --add:#1F6F4A; --add-bg:#DFF0E5; --del:#9B3220; --del-bg:#F8E4DF;
  --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  --sans:ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#121614; --panel:#191F1D; --ink:#E7EAE7; --ink-soft:#B3BAB6;
  --muted:#8C948F; --rule:#2E3835; --rule-soft:#222A28; --accent:#63B4AB;
  --differs:#E08A72; --differs-bg:#3A211B; --same:#79C7A0;
  --add:#79C7A0; --add-bg:#16301F; --del:#E08A72; --del-bg:#361D18;
}}
:root[data-theme=dark]{
  --paper:#121614; --panel:#191F1D; --ink:#E7EAE7; --ink-soft:#B3BAB6;
  --muted:#8C948F; --rule:#2E3835; --rule-soft:#222A28; --accent:#63B4AB;
  --differs:#E08A72; --differs-bg:#3A211B; --same:#79C7A0;
  --add:#79C7A0; --add-bg:#16301F; --del:#E08A72; --del-bg:#361D18;
}
:root[data-theme=light]{
  --paper:#FBFAF7; --panel:#F3F2ED; --ink:#16191A; --ink-soft:#4A514D;
  --muted:#6C736E; --rule:#C8CEC9; --rule-soft:#E4E7E2; --accent:#0E5A56;
  --differs:#9B3220; --differs-bg:#F6E3DE; --same:#2C6A4E;
  --add:#1F6F4A; --add-bg:#DFF0E5; --del:#9B3220; --del-bg:#F8E4DF;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55}
.wrap{max-width:88rem;margin:0 auto;padding:2.75rem 1.25rem 5rem}
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
.skind{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);border:1px solid var(--rule);padding:.12rem .38rem;border-radius:2px}
.spages{font-family:var(--mono);font-size:.72rem;color:var(--muted);white-space:nowrap}
.sdetail{font-size:.72rem;color:var(--differs)}
.chip{font-size:.62rem;letter-spacing:.08em;text-transform:uppercase;font-weight:600;
  padding:.14rem .42rem;border:1px solid currentColor;border-radius:2px;white-space:nowrap}
.chip--same{color:var(--muted)} .chip--changed{color:var(--differs)}
.chip--added{color:var(--add)} .chip--removed{color:var(--del)}

.scroll{overflow-x:auto}
table.rows{border-collapse:collapse;width:100%;min-width:max-content}
table.rows td{vertical-align:top;padding:.3rem .55rem;border-bottom:1px solid var(--rule-soft)}
.gut{font-family:var(--mono);font-size:.68rem;color:var(--muted);text-align:right;
  width:2.4rem;white-space:nowrap;user-select:none}
.side{width:50%;min-width:18rem}
.side--empty{background:repeating-linear-gradient(135deg,transparent,transparent 5px,
  var(--rule-soft) 5px,var(--rule-soft) 6px)}
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
.ch{font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;font-weight:600;
  color:var(--accent);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cgut{font-size:.6rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  text-align:right}
/* Two real columns when the table is narrow enough for them to fit. */
.rows--cols .side+.side{border-left:1px solid var(--rule)}

.prose{display:grid;grid-template-columns:2.4rem 1fr 1fr 2.4rem;gap:.55rem;
  border-bottom:1px solid var(--rule-soft);padding:.3rem 0}
.prose .side{width:auto;min-width:0}
.para{margin:0;font-size:.87rem;max-width:68ch}
del{background:var(--del-bg);color:var(--del);text-decoration:line-through}
ins{background:var(--add-bg);color:var(--add);text-decoration:none}

body.hide-same .sec--same{display:none}
body.hide-oneside .sec--added,body.hide-oneside .sec--removed{display:none}
.toc{border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);
  margin:0 0 2rem;padding:.9rem 0 1rem}
.toc h2{font-family:var(--serif);font-weight:400;font-size:1rem;margin:0 0 .6rem}
.toc ol{margin:0;padding:0;list-style:none;display:grid;gap:.28rem;
  grid-template-columns:repeat(auto-fill,minmax(24rem,1fr))}
.toc a{color:var(--ink-soft);text-decoration:none;font-size:.82rem;display:flex;gap:.5rem}
.toc a:hover,.toc a:focus-visible{color:var(--accent);text-decoration:underline}
.toc .n{font-family:var(--mono);font-size:.7rem;color:var(--differs);
  min-width:3.4rem;text-align:right;flex:none}
.toc .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
summary:focus-visible,a:focus-visible,input:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
html{scroll-behavior:smooth}
@media (max-width:60rem){
  .docs,.prose{grid-template-columns:1fr}
  .prose .gut{display:none}
}
footer{border-top:1px solid var(--rule);margin-top:2.5rem;padding-top:1rem;
  color:var(--muted);font-size:.8rem;max-width:72ch}
"""

_JS = """
for (const [id, cls] of [['hide-same', 'hide-same'], ['hide-oneside', 'hide-oneside']]) {
  document.getElementById(id).addEventListener('change', function (e) {
    document.body.classList.toggle(cls, e.target.checked);
  });
}
"""


def write_side_by_side(
    sections: list[Section], meta: Meta, output_path: str
) -> str:
    s = meta.summary
    tags = meta.tags
    identical_sections = sum(1 for x in sections if x.status == "same")
    one_sided = sum(1 for x in sections if x.status in ("added", "removed"))

    body = "".join(_section_html(x, i, tags) for i, x in enumerate(sections))

    # An index of the sections that differ on both sides — the ones a reviewer
    # has to look at. 700 sections is too many to scroll hunting for them.
    differing = [(i, x) for i, x in enumerate(sections) if x.status == "changed"]
    toc_items = "".join(
        f'<li><a href="#s{i}"><span class="n">'
        f'{x.changed}/{len(x.pairs)}</span>'
        f'<span class="t">{_e(x.title) or "&nbsp;"}</span></a></li>'
        for i, x in differing
    )
    toc = (
        f'<nav class="toc"><h2>{len(differing)} sections differ on both sides</h2>'
        f"<ol>{toc_items}</ol></nav>"
        if differing
        else ""
    )

    page = f"""<title>Side-by-side comparison — {_e(meta.pdf_a.rsplit('/', 1)[-1])} vs {_e(meta.pdf_b.rsplit('/', 1)[-1])}</title>
<style>{_CSS}</style>
<div class="wrap">
  <p class="eyebrow">Side-by-side &middot; reconstructed paragraphs and tables</p>
  <h1>Every paragraph and table, matched by content and placed side by side</h1>
  <p class="standfirst">Matched on what each passage says, not where it sits, because
  the two files paginate differently. A is on the left, B on the right.</p>

  <div class="caution">
    <strong>This view infers structure.</strong> A PDF holds glyphs at coordinates, not
    paragraphs and tables, so the blocks below are reconstructed from baselines, gaps and
    column positions, and the pairing is a similarity judgement. Read a difference as
    &ldquo;look at this&rdquo;, not as proof. For proof, use the exact comparison, which
    compares bytes, glyphs and pixels and infers nothing.
  </div>

  <div class="docs">
    <div><b>{_e(meta.label_a)}</b>
      <code>{_e(meta.pdf_a.rsplit('/', 1)[-1])}</code><br>
      {meta.pages_a} pages{f" &middot; {_e(meta.producer_a)}" if meta.producer_a else ""}</div>
    <div><b>{_e(meta.label_b)}</b>
      <code>{_e(meta.pdf_b.rsplit('/', 1)[-1])}</code><br>
      {meta.pages_b} pages{f" &middot; {_e(meta.producer_b)}" if meta.producer_b else ""}</div>
  </div>

  <div class="stats">
    <div class="stat"><b>{s.matched:,}</b><span>passages matched</span></div>
    <div class="stat stat--bad"><b>{s.changed:,}</b><span>matched but differing</span></div>
    <div class="stat stat--bad"><b>{s.changed_figures:,}</b><span>figure cells differing</span></div>
    <div class="stat stat--good"><b>{s.unchanged:,}</b><span>matched and agreeing</span></div>
    <div class="stat"><b>{s.only_in_a:,}</b><span>only in A</span></div>
    <div class="stat"><b>{s.only_in_b:,}</b><span>only in B</span></div>
  </div>

  <div class="bar">
    <label><input type="checkbox" id="hide-same"> Hide {identical_sections:,} identical</label>
    <label><input type="checkbox" id="hide-oneside"> Hide {one_sided:,} one-sided</label>
    <span class="key"><i style="background:var(--differs-bg)"></i> figure differs</span>
    <span class="key"><i style="background:var(--del-bg)"></i> text only in A</span>
    <span class="key"><i style="background:var(--add-bg)"></i> text only in B</span>
    <span>{s.paragraphs_matched:,} paragraphs &middot; {s.rows_matched:,} table rows</span>
  </div>

{toc}
  <div class="colhead">
    <span class="cgut">pg</span>
    <span class="ch">{_e(meta.label_a)}</span>
    <span class="ch">{_e(meta.label_b)}</span>
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
<script>{_JS}</script>
"""
    with open(output_path, "w") as fh:
        fh.write(page)
    return output_path
