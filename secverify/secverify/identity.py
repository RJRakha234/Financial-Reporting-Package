"""Phase 3 (toolsigma): geometry-bound identifier ↔ name association (B10).

A statutory identifier placed against the wrong person — a director's DIN
transposed to another director's valid number — passes every text check,
because both numbers and both names exist in the document.  Binding a number
to its person is the only way to catch it.

Doing that from *flattened* PDF text fails: signature blocks are **columnar**
(each signatory is a vertical column — name, then title, then DIN — and the
columns sit side by side).  Reading-order flattening collapses the four
columns onto shared lines, so the name nearest a DIN in the text is usually a
*different* signatory.  This module instead binds by **word geometry**: each
identifier is tied to the name whose column (x-position) it sits under, which
reconstructs the true pairing.

The HTML side keeps text-proximity binding — an HTML signature block is a
single linear flow (name → title → DIN), so the preceding name *is* the right
one.  Only a genuine mismatch (the PDF column-name and the HTML preceding-name
share no word) is reported, and only when both sides bind the number to a real
name — so column bleed can only make the check *more* conservative, never
raise a false positive.
"""

from __future__ import annotations

import re

from .phase1 import _TITLES, _IDENT_RE, _nearest_name

#: an identifier keyword that anchors a column
_KW_RE = re.compile(r"^(DIN|UDIN|Membership|Firm)", re.I)
#: a single name word ("Nandan", "M.", "Nilekani", "Parikh")
_NAME_TOK_RE = re.compile(r"^[A-Z][a-zA-Z.'-]+$")
#: an identifier value fragment (must contain a digit)
_VAL_RE = re.compile(r"[A-Za-z0-9/\-]*\d[A-Za-z0-9/\-]*")
#: audit-firm words that are never a personal name
_FIRM_WORDS = {
    "haskins", "sells", "deloitte", "chartered", "accountants", "llp",
    "behalf", "haskins", "walker", "chandiok", "price", "waterhouse",
}
#: how far right of a keyword a name may start before it is another column
_COL_WIDTH = 95
#: how far above a keyword its name may sit (a couple of title lines)
_MAX_NAME_RISE = 75


def _name_words(tokens) -> set[str]:
    out: set[str] = set()
    for t in tokens:
        lw = re.sub(r"[^a-z]", "", t.lower())
        if len(lw) >= 3 and lw not in _TITLES and lw not in _FIRM_WORDS:
            out.add(lw)
    return out


def _page_geom_bindings(words) -> dict[str, set[str]]:
    """``{identifier value: name words}`` from one page's word geometry."""
    bands: dict[int, list] = {}
    for w in words:
        bands.setdefault(round(w["top"] / 3.0), []).append(w)
    band_top = {b: min(x["top"] for x in bands[b]) for b in bands}
    anchors = sorted({round(w["x0"]) for w in words if _KW_RE.match(w["text"])})

    out: dict[str, set[str]] = {}
    for w in words:
        if not _KW_RE.match(w["text"]):
            continue
        anchor, ktop = w["x0"], w["top"]
        nxt = [a for a in anchors if a > anchor + 20]
        right = min(anchor + _COL_WIDTH, (nxt[0] - 8) if nxt else anchor + _COL_WIDTH)

        # the identifier value: first digit-bearing token to the right on the line
        val = None
        same = sorted(
            (x for x in words
             if abs(x["top"] - ktop) < 4 and anchor - 1 <= x["x0"] < anchor + 130),
            key=lambda x: x["x0"],
        )
        for x in same:
            if _KW_RE.match(x["text"]) or not re.search(r"\d", x["text"]):
                continue
            m = _VAL_RE.search(x["text"])
            if m:
                v = re.sub(r"[^A-Za-z0-9]", "", m.group(0)).upper()
                if len(v) >= 4:
                    val = v
                    break
        if not val:
            continue

        # the name: nearest name-bearing band above, within this column
        for b in sorted(bands, key=lambda b: -band_top[b]):
            if band_top[b] >= ktop - 2 or ktop - band_top[b] > _MAX_NAME_RISE:
                continue
            col = [
                x["text"]
                for x in bands[b]
                if anchor - 12 <= x["x0"] <= right and _NAME_TOK_RE.match(x["text"])
            ]
            nw = _name_words(col)
            if nw:
                out.setdefault(val, set()).update(nw)
                break
    return out


def _pdf_geom_bindings(pdf_paths) -> dict[str, set[str]]:
    import pdfplumber

    out: dict[str, set[str]] = {}
    for path in pdf_paths:
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    words = page.extract_words(x_tolerance=1)
                    if not any(_KW_RE.match(w["text"]) for w in words):
                        continue
                    for k, v in _page_geom_bindings(words).items():
                        out.setdefault(k, set()).update(v)
        except Exception:
            continue  # unreadable/missing source → abstain, never crash
    return out


def _html_bindings(html_text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for m in _IDENT_RE.finditer(html_text):
        val = re.sub(r"[^A-Za-z0-9]", "", m.group(2)).upper()
        names = _nearest_name(html_text[max(0, m.start() - 80): m.start()])
        out.setdefault(val, set()).update(names)
    return out


def check_identifier_geometry(pdf_paths, html_text, add_issue) -> None:
    """Flag an HTML identifier bound to a different person than the PDF.

    Yields at most one issue per identifier value, only when the PDF (by
    column geometry) and the HTML (by preceding name) both bind the number to
    a real name and those names share **no** word.
    """
    if not pdf_paths:
        return
    pdf_ids = _pdf_geom_bindings(pdf_paths)
    if not pdf_ids:
        return
    html_ids = _html_bindings(html_text)
    for val, html_names in html_ids.items():
        pdf_names = pdf_ids.get(val)
        if pdf_names and html_names and not (pdf_names & html_names):
            add_issue(
                "identifier-name",
                "error",
                f"{val} ↔ {', '.join(sorted(html_names))}",
                f"Identifier association — in the HTML {val} sits against "
                f"“{', '.join(sorted(html_names))}”, but in the PDF the same "
                f"number belongs to “{', '.join(sorted(pdf_names))}” (matched by "
                "its position in the signature block). The number appears to be "
                "placed against the wrong person; correct it.",
            )
