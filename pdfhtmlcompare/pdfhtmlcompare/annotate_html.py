"""Write a copy of the filed HTML with an inline ✓ / ✗ comment on financial rows.

The HTML is re-serialised verbatim (tags, attributes and styling preserved) and a
small status badge is injected at the end of each **financial** row: a green ✓
where the row's numbers and wording match the PDF, a red ✗ where a number or word
differs, a blue ✗ where the line is not in the PDF, and an amber ⚠ where a line
present in the PDF is missing here. Non-financial content (prose, headings,
contents, signatures) is left unmarked — only the financial tables are in scope.

The walk uses the same row rules as :mod:`pdfhtmlcompare.htmldoc`, so the Nth
logical line annotated is the Nth line the comparison saw.
"""

from html import unescape
from html.parser import HTMLParser

from .compare import CHANGED, LINE_EXTRA, LINE_MISSING, VALIDATED, ComparisonResult
from .htmldoc import BREAK_TAGS, CELL_TAGS, SKIP_TAGS, WS_RE

_GOOD = "#0a7a0a"
_BAD = "#b00020"
_EXTRA = "#1551b5"
_WARN = "#9a6700"
_RANK = {VALIDATED: 0, CHANGED: 1, LINE_EXTRA: 2}

_LEGEND = (
    '<div style="font:600 12px Arial,sans-serif;background:#fafafa;border:1px solid #ddd;'
    'padding:8px 12px;margin:6px 0;color:#222">'
    'pdfhtmlcompare — financial tables vs published PDF: '
    f'<span style="color:{_GOOD}">✓ matches</span> &nbsp; '
    f'<span style="color:{_BAD}">✗ number/wording differs</span> &nbsp; '
    f'<span style="color:{_EXTRA}">✗ line only in HTML</span> &nbsp; '
    f'<span style="color:{_WARN}">⚠ line missing (present in PDF)</span>'
    "</div>"
)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _worse(a, b):
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def _build_annotations(result: ComparisonResult):
    ann: dict[int, dict] = {}
    leading: list[str] = []

    def slot(i):
        return ann.setdefault(i, {"status": VALIDATED, "msgs": [], "warn": []})

    for r in result.rows:
        if r.html_line is not None:
            s = slot(r.html_line.index)
            s["status"] = _worse(s["status"], r.status)
            if r.status != VALIDATED:
                s["msgs"].extend(r.messages())
        elif r.status == LINE_MISSING:
            msg = r.findings[0].message()
            if r.html_anchor is not None:
                slot(r.html_anchor)["warn"].append(msg)
            else:
                leading.append(msg)
    return ann, leading


def _render_badge(slot: dict) -> str:
    status = slot["status"]
    parts = []
    if status == VALIDATED:
        parts.append(f'<span style="color:{_GOOD};font-weight:bold" title="matches PDF">✓</span>')
    elif status == LINE_EXTRA:
        parts.append(f'<span style="color:{_EXTRA};font-weight:bold">✗ not in PDF</span>')
    else:
        detail = "; ".join(_esc(m) for m in slot["msgs"]) or "differs from PDF"
        parts.append(f'<span style="color:{_BAD};font-weight:bold">✗ {detail}</span>')
    for w in slot["warn"]:
        parts.append(f'<span style="color:{_WARN};font-weight:bold">⚠ {_esc(w)}</span>')
    return (
        '<span style="font:600 11px Arial,sans-serif;white-space:normal"> &nbsp;'
        + " &nbsp; ".join(parts) + "</span>"
    )


class _Annotator(HTMLParser):
    def __init__(self, ann, leading):
        super().__init__(convert_charrefs=False)
        self.ann = ann
        self.leading = leading
        self.out: list[str] = []
        self.buf: list[str] = []
        self.idx = 0
        self.skip = 0
        self.table_depth = 0

    def _emit(self, s):
        self.out.append(s)

    def _boundary(self, tag, is_start):
        text = WS_RE.sub(" ", " ".join(self.buf)).strip()
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

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in BREAK_TAGS:
            self._boundary(t, True)
        self._emit(self.get_starttag_text() or f"<{tag}>")
        if t in SKIP_TAGS:
            self.skip += 1
        elif t == "table":
            self.table_depth += 1
        elif t in CELL_TAGS and self.skip == 0:
            self.buf.append(" ")
        elif t == "body":
            self._emit(_LEGEND)
            for msg in self.leading:
                self._emit(f'<div style="font:600 12px Arial;color:{_WARN}">⚠ {_esc(msg)}</div>')

    def handle_startendtag(self, tag, attrs):
        t = tag.lower()
        if t in BREAK_TAGS:
            self._boundary(t, True)
        self._emit(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in BREAK_TAGS:
            self._boundary(t, False)
        self._emit(f"</{tag}>")
        if t in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif t == "table":
            self.table_depth = max(0, self.table_depth - 1)
        elif t in CELL_TAGS and self.skip == 0:
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


def write_commented_html(html_path, output_html, result, encoding="utf-8") -> str:
    with open(html_path, "r", encoding=encoding, errors="replace") as fh:
        text = fh.read()
    with open(output_html, "w", encoding=encoding) as fh:
        fh.write(annotate_html(text, result))
    return output_html
