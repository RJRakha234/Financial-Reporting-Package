"""Write a copy of the filed HTML with an inline ✓ / ✗ comment on every line.

The HTML is re-serialised verbatim and a badge is injected at the end of each
content line: a green ✓ where every word and number of the line was found in the
PDF, a red ✗ where something differs or was added, and an amber ⚠ where content
present in the PDF is missing at that point. The walk uses the same row rules and
the same ``<body>`` gating as :mod:`pdfhtmlcompare.htmldoc`, so the Nth line it
annotates is the Nth line the comparison saw.
"""

from html import unescape
from html.parser import HTMLParser

from .compare import ADDED, CHANGED, MISSING, ComparisonResult
from .htmldoc import BREAK_TAGS, CELL_TAGS, SKIP_TAGS, WS_RE, gate_on_body

_GOOD = "#0a7a0a"
_BAD = "#b00020"
_WARN = "#9a6700"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_annotations(result: ComparisonResult):
    ann: dict[int, dict] = {}
    leading: list[str] = []

    # Every content line starts "ok" (a ✓); findings downgrade it.
    for line in result.html_lines:
        if line.tokens:
            ann[line.id] = {"ok": True, "msgs": [], "warn": []}

    for f in result.findings:
        if f.kind in (CHANGED, ADDED):
            for lid in f.html_line_ids:
                s = ann.setdefault(lid, {"ok": True, "msgs": [], "warn": []})
                s["ok"] = False
                if f.message() not in s["msgs"]:
                    s["msgs"].append(f.message())
        elif f.kind == MISSING:
            if f.html_anchor is not None and f.html_anchor in ann:
                ann[f.html_anchor]["warn"].append(f.message())
            elif f.html_anchor is None:
                leading.append(f.message())
    return ann, leading


def _legend(result: ComparisonResult) -> str:
    return (
        '<div style="font:600 12px Arial,sans-serif;background:#fafafa;border:1px solid #ddd;'
        'padding:8px 12px;margin:6px 0;color:#222">'
        'pdfhtmlcompare — whole content vs published PDF '
        f'({result.coverage * 100:.1f}% of PDF matched): '
        f'<span style="color:{_GOOD}">✓ matches</span> &nbsp; '
        f'<span style="color:{_BAD}">✗ differs / added</span> &nbsp; '
        f'<span style="color:{_WARN}">⚠ content missing (present in PDF)</span></div>'
    )


def _render_badge(slot: dict) -> str:
    parts = []
    if slot["ok"]:
        parts.append(f'<span style="color:{_GOOD};font-weight:bold" title="matches PDF">✓</span>')
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
    def __init__(self, ann, leading, gate_body):
        super().__init__(convert_charrefs=False)
        self.ann = ann
        self.leading = leading
        self.out: list[str] = []
        self.buf: list[str] = []
        self.idx = 0
        self.skip = 0
        self.table_depth = 0
        self.gate_body = gate_body
        self.in_body = not gate_body
        self.legend_html = ""

    def _emit(self, s):
        self.out.append(s)

    def _boundary(self, tag, is_start):
        text = WS_RE.sub(" ", " ".join(self.buf)).strip()
        self.buf = []
        if not text or not self.in_body:
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
        if t == "body":
            self.in_body = True
            self._emit(self.legend_html)
            for msg in self.leading:
                self._emit(f'<div style="font:600 12px Arial;color:{_WARN}">⚠ {_esc(msg)}</div>')
        if t in SKIP_TAGS:
            self.skip += 1
        elif t == "table":
            self.table_depth += 1
        elif t in CELL_TAGS and self.skip == 0:
            self.buf.append(" ")

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
        if self.skip == 0 and self.in_body and data.strip():
            self.buf.append(data)

    def handle_entityref(self, name):
        self._emit(f"&{name};")
        if self.skip == 0 and self.in_body:
            self.buf.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        self._emit(f"&#{name};")
        if self.skip == 0 and self.in_body:
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
    parser = _Annotator(ann, leading, gate_on_body(html_text))
    parser.legend_html = _legend(result)
    parser.feed(html_text)
    parser.close()
    return "".join(parser.out)


def write_commented_html(html_path, output_html, result, encoding="utf-8") -> str:
    with open(html_path, "r", encoding=encoding, errors="replace") as fh:
        text = fh.read()
    with open(output_html, "w", encoding=encoding) as fh:
        fh.write(annotate_html(text, result))
    return output_html
