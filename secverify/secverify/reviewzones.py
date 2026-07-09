"""Blue manual-review overlay for Class 2 / Class 3 error surfaces.

The engine cannot *verdict* two error families, because they preserve every
token in the document (nothing is missing to react to):

* **Class 2 — token-preserving rearrangements**: two figures swapped in running
  prose, a value tied to the wrong label where that pair also exists elsewhere.
* **Class 3 — wrong cross-reference**: "refer Note 12" changed to "Note 21"
  when both notes exist.

For a reviewer who does not want to take *any* chance on these, this overlay
paints the *locations where such an error could hide* — not an error claim, a
"verify this by eye" marker — in **blue**:

* every **cross-reference phrase** ("refer to Note 12", "see Schedule 3");
* every **figure that sits in prose** (outside a ``<table>``).  Figures inside
  tables are already positionally scrutinised by the row-value and grid checks;
  prose figures are validated by presence only, so a prose swap would pass —
  those are the ones to eyeball.

Blue never overrides a red finding: a genuine discrepancy stays red.
"""

from __future__ import annotations

import re

from bs4 import NavigableString

from .numbers import is_significant, iter_tokens

_NOTE_REF_RE = re.compile(r"^\(?\d{1,2}\.\d{1,2}\)?$")


def _is_review_figure(text: str) -> bool:
    """True for a real financial figure worth eyeballing — a significant
    amount, a currency figure, or a percentage.  Dates, years, note-reference
    numbers ("2.12") and small ordinals are excluded so the overlay marks
    line-item money, not furniture."""
    t = text.strip()
    if _NOTE_REF_RE.match(t):
        return False
    if "%" in t and re.search(r"\d", t):
        return True
    if re.search(r"[₹$]", t) and re.search(r"\d", t):
        return True
    for _s, _e, tok, key in iter_tokens(t):
        if is_significant(key, tok):
            return True
    return False

_XREF_RE = re.compile(
    r"\b(?:refer(?:\s+to)?\s+note|see\s+note|refer\s+note|note\s+no\.?|"
    r"as\s+per\s+note|schedule|annexure)\b[\s.]*\d{0,3}[A-Za-z.\d]*",
    re.I,
)


def _mark_prose_figures(soup, root) -> int:
    """Re-flag every validated (green) figure that is NOT inside a table as a
    blue manual-review figure.  Returns the count marked."""
    n = 0
    for span in root.find_all("span", class_="secv-num-ok"):
        if span.find_parent("table") is not None:
            continue  # in-table figures are positionally checked already
        if not _is_review_figure(span.get_text()):
            continue  # a date/year/note-ref, not a line-item figure
        classes = span.get("class", [])
        span["class"] = [c for c in classes if c != "secv-num-ok"] + ["secv-num-review"]
        span["title"] = (
            "Manual-review zone — this figure is validated as present in the "
            "PDF, but it sits in prose, so its placement is not position-checked. "
            "Confirm by eye that it is against the right item."
        )
        n += 1
    return n


def _mark_cross_references(soup, root) -> int:
    """Wrap every cross-reference phrase in a blue manual-review span."""
    n = 0
    for text_node in list(root.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue
        if text_node.find_parent(["script", "style"]) is not None:
            continue
        if text_node.find_parent(class_="secv-xref-review") is not None:
            continue
        s = str(text_node)
        if not _XREF_RE.search(s):
            continue
        parts = []
        last = 0
        for m in _XREF_RE.finditer(s):
            if m.start() > last:
                parts.append(soup.new_string(s[last:m.start()]))
            span = soup.new_tag("span", **{"class": "secv-xref-review"})
            span.string = m.group(0)
            span["title"] = (
                "Manual-review zone — verify this note/schedule reference points "
                "to the correct place; the tool cannot confirm cross-references."
            )
            parts.append(span)
            last = m.end()
            n += 1
        if last < len(s):
            parts.append(soup.new_string(s[last:]))
        text_node.replace_with(*parts)
    return n


def mark_review_zones(soup, root) -> tuple[int, int]:
    """Apply the blue overlay.  Returns ``(prose_figures, cross_references)``."""
    figs = _mark_prose_figures(soup, root)
    xrefs = _mark_cross_references(soup, root)
    return figs, xrefs
