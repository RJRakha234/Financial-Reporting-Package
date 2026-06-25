"""Turn an SEC-style HTML filing into ordered logical lines of text.

The HTML that is filed on EDGAR is a conversion of the very same statements
published as a PDF, so the *content order* is preserved even though the layout
is reflowed. We therefore reduce the HTML to a flat list of logical lines — one
per table row, heading, or paragraph — which the comparison engine then aligns,
number-by-number and word-by-word, against the lines read from the PDF.

Only the Python standard library is used (``html.parser``); no network access
and no third-party HTML dependency, in keeping with the rest of fincheck.
"""

import re
from html.parser import HTMLParser

# Tags whose start or end ends the current logical line. Table rows and cells
# matter most for financials; block/heading tags keep prose lines separate.
_BREAK_TAGS = {
    "tr", "table", "thead", "tbody", "tfoot", "caption",
    "p", "div", "br", "li", "ul", "ol", "dl", "dt", "dd",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "footer", "section", "article", "blockquote",
    "title", "hr",
}
# Tags we drop entirely, content and all.
_SKIP_TAGS = {"script", "style", "head", "noscript", "svg"}
# Within a row, a cell boundary is just a separator, not a new line.
_CELL_TAGS = {"td", "th"}

_WS_RE = re.compile(r"[\s   ]+")


class _LineCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0

    def _flush(self) -> None:
        text = _WS_RE.sub(" ", " ".join(self._buf)).strip()
        if text:
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BREAK_TAGS:
            self._flush()
        elif tag in _CELL_TAGS:
            self._buf.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BREAK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BREAK_TAGS:
            self._flush()
        elif tag in _CELL_TAGS:
            self._buf.append(" ")

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def extract_lines_from_html(html_text: str) -> list[str]:
    """Return the filing's text as ordered logical lines (one per row/block)."""
    parser = _LineCollector()
    parser.feed(html_text)
    parser.close()
    return parser.lines


def read_html_lines(path: str, encoding: str = "utf-8") -> list[str]:
    """Read an HTML file from disk and return its logical lines."""
    with open(path, "r", encoding=encoding, errors="replace") as fh:
        return extract_lines_from_html(fh.read())
