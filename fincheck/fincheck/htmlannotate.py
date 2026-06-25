"""Write a copy of the filed HTML with an inline ``✓ / ✗`` comment on every row.

This re-serialises the original HTML verbatim (tags, attributes, and styling are
preserved) and injects a small status badge at the end of each logical line — a
green ✓ where the row matches the published PDF, and a red/blue/amber note where
a figure changed, a cell or a whole row is missing, or wording differs. Dropped
rows (present in the PDF, absent from the HTML) are flagged in place against the
row that precedes them.

The parser walks the document with the *same* row rules as
:mod:`fincheck.htmlextract`, so the Nth logical line it annotates is the Nth line
the comparison engine saw — the statuses line up exactly.
"""

from html import unescape
from html.parser import HTMLParser

from .compare import (
    LINE_CHANGED,
    ROW_EXTRA,
    ROW_MISSING,
    VALIDATED,
    ComparisonResult,
)
from .htmlextract import _BREAK_TAGS, _CELL_TAGS, _SKIP_TAGS, _WS_RE

_STATUS_RANK = {VALIDATED: 0, LINE_CHANGED: 1, ROW_EXTRA: 2}

_GOOD = "#0a7a0a"
_BAD = "#b00020"
_EXTRA = "#1551b5"
_WARN = "#9a6700"

_LEGEND = (
    '<div style="font:600 12px Arial,sans-serif;background:#fafafa;border:1px solid #ddd;'
    'padding:8px 12px;margin:6px 0;color:#222">'
    'fincheck comparison vs published PDF: '
    f'<span style="color:{_GOOD}">✓ matches</span> &nbsp; '
    f'<span style="color:{_BAD}">✗ figure/wording differs or cell missing</span> &nbsp; '
    f'<span style="color:{_EXTRA}">✗ row only in HTML</span> &nbsp; '
    f'<span style="color:{_WARN}">⚠ row dropped (present in PDF)</span>'
    "</div>"
)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _worse(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


def _build_annotations(result: ComparisonResult):
    """html line index -> {'status', 'msgs', 'warn'}, plus leading warnings."""
    ann: dict[int, dict] = {}
    leading: list[str] = []

    def slot(i):
        return ann.setdefault(i, {"status": VALIDATED, "msgs": [], "warn": []})

    for r in result.line_results:
        if r.html_line is not None:
            s = slot(r.html_line.index)
            s["status"] = _worse(s["status"], r.status)
            if r.status != VALIDATED:
                s["msgs"].extend(r.messages())
        elif r.status == ROW_MISSING:
            msg = r.diffs[0].message()
            if r.html_anchor is not None:
                slot(r.html_anchor)["warn"].append(msg)
            else:
                leading.append(msg)
    return ann, leading


def _render_badge(slot: dict) -> str:
    parts: list[str] = []
    status = slot["status"]
    msgs = slot["msgs"]
    if status == VALIDATED:
        parts.append(f'<span style="color:{_GOOD};font-weight:bold" title="matches PDF">✓</span>')
    elif status == ROW_EXTRA:
        parts.append(f'<span style="color:{_EXTRA};font-weight:bold">✗ not in PDF</span>')
    else:  # LINE_CHANGED
        detail = "; ".join(_esc(m) for m in msgs) or "differs from PDF"
        parts.append(f'<span style="color:{_BAD};font-weight:bold">✗ {detail}</span>')
    for w in slot["warn"]:
        parts.append(f'<span style="color:{_WARN};font-weight:bold">⚠ {_esc(w)}</span>')
    return (
        '<span style="font:600 11px Arial,sans-serif;white-space:normal"> &nbsp;'
        + " &nbsp; ".join(parts)
        + "</span>"
    )


class _Annotator(HTMLParser):
    def __init__(self, ann: dict, leading: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.ann = ann
        self.leading = leading
        self.out: list[str] = []
        self.buf: list[str] = []
        self.idx = 0
        self.skip = 0
        self.table_depth = 0

    # -- output helpers --
    def _emit(self, s: str) -> None:
        self.out.append(s)

    def _boundary(self, tag: str, is_start: bool) -> None:
        text = _WS_RE.sub(" ", " ".join(self.buf)).strip()
        self.buf = []
        if not text:
            return
        i = self.idx
        self.idx += 1
        slot = self.ann.get(i)
        if not slot:
            return
        badge = _render_badge(slot)
        if self.table_depth > 0 and tag == "tr" and not is_start:
            self._emit(f'<td style="border:none">{badge}</td>')
        elif self.table_depth > 0 and tag in ("tr", "table"):
            self._emit(f'<tr><td colspan="99" style="border:none">{badge}</td></tr>')
        else:
            self._emit(badge)

    # -- tag handlers (verbatim pass-through + boundary injection) --
    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in _BREAK_TAGS:
            self._boundary(t, True)
        self._emit(self.get_starttag_text() or f"<{tag}>")
        if t in _SKIP_TAGS:
            self.skip += 1
        elif t == "table":
            self.table_depth += 1
        elif t in _CELL_TAGS and self.skip == 0:
            self.buf.append(" ")
        elif t == "body":
            self._emit(_LEGEND)
            for msg in self.leading:
                self._emit(
                    f'<div style="font:600 12px Arial;color:{_WARN}">⚠ {_esc(msg)}</div>'
                )

    def handle_startendtag(self, tag, attrs):
        t = tag.lower()
        if t in _BREAK_TAGS:
            self._boundary(t, True)
        self._emit(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in _BREAK_TAGS:
            self._boundary(t, False)
        self._emit(f"</{tag}>")
        if t in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif t == "table":
            self.table_depth = max(0, self.table_depth - 1)
        elif t in _CELL_TAGS and self.skip == 0:
            self.buf.append(" ")

    def handle_data(self, data):
        self._emit(data)
        if self.skip == 0 and data.strip():
            self.buf.append(data)

    def handle_entityref(self, name):
        self._emit(f"&{name};")
        if self.skip == 0:
            self.buf.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        self._emit(f"&#{name};")
        if self.skip == 0:
            self.buf.append(unescape(f"&#{name};"))

    def handle_comment(self, data):
        self._emit(f"<!--{data}-->")

    def handle_decl(self, decl):
        self._emit(f"<!{decl}>")

    def handle_pi(self, data):
        self._emit(f"<?{data}>")

    def unknown_decl(self, data):
        self._emit(f"<![{data}]>")

    def close(self):
        super().close()
        self._boundary("", False)


def annotate_html(html_text: str, result: ComparisonResult) -> str:
    ann, leading = _build_annotations(result)
    parser = _Annotator(ann, leading)
    parser.feed(html_text)
    parser.close()
    return "".join(parser.out)


def write_commented_html(
    html_path: str, output_html: str, result: ComparisonResult, encoding: str = "utf-8"
) -> str:
    with open(html_path, "r", encoding=encoding, errors="replace") as fh:
        html_text = fh.read()
    annotated = annotate_html(html_text, result)
    with open(output_html, "w", encoding=encoding) as fh:
        fh.write(annotated)
    return output_html
