"""Read an SEC-style HTML filing into ordered logical lines, each with figures.

The HTML filed on EDGAR is a conversion of the same statements published as a
PDF, so the content order is preserved even though the layout is reflowed. We
reduce the HTML to a flat list of logical lines — one per table row, heading, or
paragraph — using only the standard library's ``html.parser`` (no third-party
HTML dependency, no network access).

The tag rules below are shared with :mod:`pdfhtmlcompare.annotate_html`, which
re-walks the document with the *same* rules so the Nth line it annotates is the
Nth line read here — the row statuses line up exactly.
"""

import re
from html.parser import HTMLParser

from .model import Line, build_line
from .numbers import is_numberish, parse_number

# Tags whose start or end ends the current logical line.
BREAK_TAGS = {
    "tr", "table", "thead", "tbody", "tfoot", "caption",
    "p", "div", "br", "li", "ul", "ol", "dl", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "footer", "section", "article", "blockquote",
    "title", "hr",
}
SKIP_TAGS = {"script", "style", "head", "noscript", "svg"}
CELL_TAGS = {"td", "th"}

WS_RE = re.compile(r"[\s   ]+")


def _line_tokens(raw: str):
    triples = []
    for tok in raw.split():
        value = parse_number(tok) if is_numberish(tok) else None
        triples.append((tok, value, None))
    return triples


def gate_on_body(html_text: str) -> bool:
    """Whether to restrict reading to inside <body> (skip EDGAR/SGML wrapper)."""
    return "<body" in html_text.lower()


class _LineCollector(HTMLParser):
    def __init__(self, gate_body: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self._gate_body = gate_body
        self._in_body = not gate_body

    def _flush(self) -> None:
        text = WS_RE.sub(" ", " ".join(self._buf)).strip()
        if text and self._in_body:
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._in_body = True
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BREAK_TAGS:
            self._flush()
        elif tag in CELL_TAGS:
            self._buf.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in BREAK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BREAK_TAGS:
            self._flush()
        elif tag in CELL_TAGS:
            self._buf.append(" ")

    def handle_data(self, data):
        if self._skip == 0 and self._in_body and data.strip():
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def extract_html_lines(html_text: str) -> list[str]:
    """The filing's text as ordered logical lines (one per row/block)."""
    parser = _LineCollector(gate_body=gate_on_body(html_text))
    parser.feed(html_text)
    parser.close()
    return parser.lines


def read_html_lines(html_path: str, encoding: str = "utf-8") -> list[Line]:
    with open(html_path, "r", encoding=encoding, errors="replace") as fh:
        raw_lines = extract_html_lines(fh.read())
    return [
        build_line(i, raw, _line_tokens(raw)) for i, raw in enumerate(raw_lines)
    ]
