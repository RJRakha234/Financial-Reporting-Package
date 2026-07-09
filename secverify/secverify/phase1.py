"""Phase 1 checks (toolalpha): reporting-period dates and identifier↔name.

Both are keyword-anchored exact checks — the low-false-positive design that
worked for the identifier census — so they add coverage without the
matching-heuristic risk that sank the count/grid experiments.
"""

from __future__ import annotations

import re

#: reporting-period phrases that must be reproduced exactly
_PERIOD_RE = re.compile(
    r"(as at|three months ended|year ended|as of|period ended)\s+"
    r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4})",
    re.I,
)
#: statutory identifiers, keyword-anchored
_IDENT_RE = re.compile(
    r"\b(DIN|UDIN|Membership\s+No|Firm'?s?\s+Registration\s+No|"
    r"Registration\s+No|PAN|CIN)\b\s*[:.]?\s*"
    r"(?=[A-Za-z0-9/\-]*\d)([A-Za-z0-9][A-Za-z0-9/\-]{3,})",
    re.I,
)
#: any explicit calendar date, used to confirm an HTML date exists in the PDF
_ANY_DATE_RE = re.compile(r"[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}", re.I)
#: capitalised name words used to bind an identifier to a person
_NAME_WORD_RE = re.compile(r"[A-Z][a-z]{2,}")
#: title / role words that are NOT part of a personal name
_TITLES = {
    "chairman", "director", "managing", "chief", "executive", "financial",
    "officer", "company", "secretary", "partner", "member", "membership",
    "board", "directors", "and", "the", "for", "din", "udin", "no",
    "place", "date", "bengaluru", "limited", "infosys", "registration",
}


def _norm_date(s: str) -> str:
    return re.sub(r"[,\s]+", " ", s).strip().lower()


def check_period_dates(pdf_text: str, html_text: str, add_issue) -> None:
    """The CURRENT/comparative reporting-period dates in the HTML must exist
    in the PDF.

    Only dates within a year of the latest date in the documents are checked
    — those are the reporting periods (current + comparative).  Historical
    dates in narrative notes ("the 2015 Plan … on March 31, 2016") are not
    reporting periods and are ignored, so they never false-flag.  The PDF
    side is ALL dates, so a date present anywhere in the PDF is accepted.
    """
    all_years = [
        int(y)
        for y in re.findall(r"\b(19|20)\d{2}\b", pdf_text + " " + html_text)
    ]
    # re.findall with a group returns the prefix; recompute full years
    all_years = [int(m.group(0)) for m in re.finditer(r"\b(?:19|20)\d{2}\b",
                                                       pdf_text + " " + html_text)]
    if not all_years:
        return
    max_year = max(all_years)
    pdf_dates = {_norm_date(m.group(0)) for m in _ANY_DATE_RE.finditer(pdf_text)}
    seen: set[str] = set()
    for m in _PERIOD_RE.finditer(html_text):
        phrase, date = m.group(1), _norm_date(m.group(2))
        yr = int(re.search(r"\d{4}", date).group(0))
        if yr < max_year - 1:
            continue  # historical narrative date, not a reporting period
        if date in pdf_dates or date in seen:
            continue
        seen.add(date)
        add_issue(
            "date",
            "error",
            f"{phrase} {m.group(2)}",
            f"Reporting period — the date “{m.group(2)}” (in “{phrase} "
            f"{m.group(2)}”) does not appear anywhere in the PDF. A wrong "
            "period or comparative date mis-states the whole column; verify.",
        )


def _name_words(window: str) -> set[str]:
    """Personal-name words (title/role words removed) from a text window."""
    return {
        w.lower()
        for w in _NAME_WORD_RE.findall(window)
        if w.lower() not in _TITLES
    }


def _identifiers_with_names(text: str) -> dict[str, set[str]]:
    """Map identifier value → the set of personal-name words bound to it."""
    out: dict[str, set[str]] = {}
    for m in _IDENT_RE.finditer(text):
        val = re.sub(r"[^A-Za-z0-9]", "", m.group(2)).upper()
        names = _name_words(text[max(0, m.start() - 80) : m.start()])
        out.setdefault(val, set()).update(names)
    return out


def check_identifier_association(pdf_text: str, html_text: str, add_issue) -> None:
    """Each HTML identifier must be bound to the same person as in the PDF.

    Catches a DIN transposed to *another director's valid* number.  Names are
    compared as word sets with titles removed, so ordering/title differences
    do not false-flag — only a total mismatch (no shared name word) is
    reported.
    """
    pdf_ids = _identifiers_with_names(pdf_text)
    html_ids = _identifiers_with_names(html_text)
    seen: set[str] = set()
    for val, html_names in html_ids.items():
        if val in seen or not html_names:
            continue
        pdf_names = pdf_ids.get(val)
        # Only assert when the identifier is bound to real names on BOTH sides
        # and they share NO name word — a strong signal of a wrong pairing.
        if pdf_names and not (html_names & pdf_names):
            seen.add(val)
            add_issue(
                "identifier-name",
                "review",
                f"{val} ↔ {', '.join(sorted(html_names))}",
                f"Identifier association — in the HTML {val} sits against "
                f"“{', '.join(sorted(html_names))}”, but in the PDF the same "
                f"number sits against “{', '.join(sorted(pdf_names))}”. It may "
                "have been placed against the wrong person; verify.",
            )
