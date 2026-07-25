"""Validate an HTML rendering against the PDF corpus and highlight it.

Every figure and every sentence of the HTML is checked against the PDF:

* **green**  — validated (figure found in the PDF / text matches the PDF);
* **amber**  — close match with differences, review manually;
* **red**    — not found in the PDF; a remark explains what to look at.

Red/amber items get a numbered marker linking into a summary panel at the
top of the page, a hover tooltip, and an HTML comment (``<!-- SECVERIFY
REMARK #n: ... -->``) placed right next to the highlighted element so the
remark survives copy/paste into other tools.
"""

from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Comment, NavigableString

from .coverage import CoverageLine, CoverageResult, HtmlCorpus, check_pdf_coverage
from .numbers import is_significant, iter_tokens
from .pdfside import PdfCorpus
from .textnorm import (
    canonical,
    find_best_match,
    smallest_subsequence_window,
    split_sentences,
    word_subsequence_span,
)

BLOCK_TAGS = ("p", "td", "th", "li", "caption", "h1", "h2", "h3", "h4", "h5", "h6", "div")
SKIP_PARENTS = {"script", "style", "title", "head"}
FUZZY_REVIEW_RATIO = 0.80  # ≥ this but not exact → amber "review"

#: a calendar date "Month DD, YYYY" or "Month YYYY" — the reporting-period
#: form that hides inside otherwise-identical wording ("quarter ended June 30,
#: 2025" vs the PDF's "…2026").  Word canonicalisation drops the digits, so a
#: wrong year in such a phrase would pass the words-only match as green; the
#: Tier-2 date guard re-checks these positionally.
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
_DATE_RE = re.compile(rf"{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}|{_MONTH}\s+\d{{4}}", re.I)


#: the EDGAR SGML submission header that precedes the exhibit's real markup:
#:   <DOCUMENT><TYPE>EX-99.1 CHARTER<SEQUENCE>2<FILENAME>exv99w01.htm
#:   <DESCRIPTION>IFRS USD PRESS RELEASE<TEXT>
#: These are SGML fields, not HTML elements, so a parser leaves their values as
#: visible text — putting "EX-99.1 CHARTER 2 exv99w01.htm IFRS USD PRESS
#: RELEASE" at the top of the document.  None of it is in the source PDF, so
#: every exhibit picked up a handful of phantom findings (two of them red) from
#: filing furniture alone.  It carries no financial content, so it is removed
#: before parsing rather than reported.
_EDGAR_HEADER_RE = re.compile(
    r"^\s*(?:<DOCUMENT>|<TYPE>|<SEQUENCE>|<FILENAME>|<DESCRIPTION>)[^\n]*\n",
    re.I | re.M,
)
_EDGAR_TEXT_OPEN_RE = re.compile(r"^\s*<TEXT>\s*$\n?", re.I | re.M)


def strip_edgar_submission_header(html_text: str) -> str:
    """Remove the EDGAR SGML submission header preceding an exhibit's markup.

    Only the wrapper fields are dropped, and only when the document actually
    opens with them — the exhibit's own markup is left untouched, so a plain
    HTML file (or one already stripped) passes through unchanged.
    """
    head = html_text[:2000]
    if not re.search(r"<(?:DOCUMENT|TYPE|SEQUENCE|FILENAME|DESCRIPTION)>", head, re.I):
        return html_text
    out = _EDGAR_HEADER_RE.sub("", html_text, count=6)
    return _EDGAR_TEXT_OPEN_RE.sub("", out, count=1)


def _dates_in(text: str) -> list[str]:
    """Ordered, normalised calendar dates in *text* (e.g. 'june 30 2025')."""
    return [
        re.sub(r"[,\s]+", " ", m.group(0)).strip().lower()
        for m in _DATE_RE.finditer(text)
    ]


#: a reporting unit-of-scale word — swapping one (crore ↔ million) mis-states
#: every figure under it by orders of magnitude while the digits are untouched
_SCALE_WORD_RE = re.compile(
    r"\b(crores?|millions?|lakhs?|lac|billions?|thousand)\b", re.I
)


def _scale_unit_swap(html_sent: str, pdf_snippet: str) -> tuple[str, str] | None:
    """``(html_unit, pdf_unit)`` if the two texts are identical APART from a
    reporting-unit word, else ``None``.

    A caption changed from "(in ₹ crore)" to "(in ₹ million)" leaves every
    digit unchanged, so the block otherwise matches — this isolates that the
    only difference is the scale word.
    """
    hu = [m.group(0).lower().rstrip("s") for m in _SCALE_WORD_RE.finditer(html_sent)]
    pu = [m.group(0).lower().rstrip("s") for m in _SCALE_WORD_RE.finditer(pdf_snippet)]
    if not hu or not pu or hu == pu:
        return None
    blank = lambda s: canonical(_SCALE_WORD_RE.sub(" unit ", s), letters_only=True)
    if blank(html_sent) == blank(pdf_snippet):
        return (", ".join(dict.fromkeys(hu)), ", ".join(dict.fromkeys(pu)))
    return None


#: a scale word that makes a small number a material amount ("8 crore")
_SCALE_AFTER = re.compile(
    r"\s*(crore|crores|lakh|lakhs|lac|million|millions|billion|billions|"
    r"thousand|trillion|mn|bn)\b",
    re.I,
)


def _is_minor(text: str, start: int, end: int, key: str, token: str) -> bool:
    """Whether a number is an immaterial list marker / year / bare count.

    These recur throughout a document, so finding the digit "somewhere" in
    the PDF is not validation — they must not be stamped green.  A number is
    NOT minor (i.e. it is a real figure) when it carries a percent sign, a
    currency symbol, or a scale word (crore/lakh/million…) — "8 crore" and
    "54%" are material even though "8" and "54" are below 100.
    """
    if is_significant(key, token):
        return False
    t = token.strip()
    if "%" in t or "₹" in t or "$" in t:
        return False
    if "₹" in text[max(0, start - 2):start] or "$" in text[max(0, start - 2):start]:
        return False
    if _SCALE_AFTER.match(text[end:end + 12]):
        return False
    return True


@dataclass
class Issue:
    num: int
    kind: str      # "figure" | "text" | "omission" | "figure-count"
    severity: str  # "error" | "review" | "caution"
    excerpt: str
    remark: str
    anchor: str
    #: triage tier: "act" = concrete discrepancy to fix; "absent" = content
    #: with no counterpart in this PDF (verify against its own source);
    #: "layout" = words verified on the cited page, print order differs;
    #: "caution" = lower-confidence possible issue (wide matrices etc.) that may
    #: include the occasional false alarm — kept out of the zero-FP "act" tier
    tier: str = "act"


@dataclass
class Result:
    issues: list[Issue] = field(default_factory=list)
    figures_total: int = 0
    figures_ok: int = 0
    text_blocks_total: int = 0
    text_blocks_ok: int = 0
    text_blocks_review: int = 0
    pdf_figures_missing: list[dict] = field(default_factory=list)
    figure_count_mismatches: list[dict] = field(default_factory=list)
    coverage: CoverageResult = field(default_factory=CoverageResult)
    html_out: str = ""

    @property
    def figures_bad(self) -> int:
        return self.figures_total - self.figures_ok

    @property
    def text_blocks_bad(self) -> int:
        return (
            self.text_blocks_total - self.text_blocks_ok - self.text_blocks_review
        )


class Annotator:
    def __init__(
        self,
        corpus: PdfCorpus,
        level: str = "base",
        pdf_paths: "list[str] | None" = None,
        review_zones: bool = False,
        strict: bool = False,
        footed: bool = False,
    ):
        self.corpus = corpus
        self.result = Result()
        self._issue_seq = 0
        self.level = level
        self.pdf_paths = pdf_paths or []
        self.review_zones = review_zones or strict
        self.strict = strict
        self.footed = footed
        self._zone_counts = (0, 0, 0, 0)

    def _at_least(self, name: str) -> bool:
        from . import LEVELS

        return LEVELS.get(self.level, 0) >= LEVELS[name]

    # -- issues ----------------------------------------------------------
    def _new_issue(
        self, kind: str, severity: str, excerpt: str, remark: str, tier: str = "act"
    ) -> Issue:
        self._issue_seq += 1
        issue = Issue(
            num=self._issue_seq,
            kind=kind,
            severity=severity,
            excerpt=" ".join(excerpt.split())[:160],
            remark=remark,
            anchor=f"secv-i{self._issue_seq}",
            tier=tier,
        )
        self.result.issues.append(issue)
        return issue

    # -- figures ---------------------------------------------------------
    def _annotate_numbers(self, soup: BeautifulSoup, root) -> None:
        html_number_keys = set()
        for node in list(root.descendants):
            if not isinstance(node, NavigableString) or isinstance(node, Comment):
                continue
            if node.find_parent(SKIP_PARENTS) is not None:
                continue
            if node.find_parent(class_="secv-marker") is not None:
                continue  # our own [n] issue markers are not figures
            text = str(node)
            tokens = list(iter_tokens(text))
            if not tokens:
                continue

            fragments = []
            cursor = 0
            replaced = False
            for start, end, token, key in tokens:
                # Leading-zero runs are identifiers (DIN 00041245, registration
                # numbers), never monetary amounts — leave them unchecked so
                # they are not flagged as "figures not in the PDF".
                if re.match(r"^\(?0\d", token.strip()):
                    continue
                # A number glued to a preceding letter is the numeric part of
                # an alphanumeric identifier/code (membership "A21918", UDIN
                # "…BMOCJH8380"), not an amount — the identifier census checks
                # those; skip here to avoid double-reporting.
                if start > 0 and text[start - 1].isalpha():
                    continue
                html_number_keys.add(key)
                ok = self.corpus.has_number(key)
                if start > cursor:
                    fragments.append(text[cursor:start])
                span = soup.new_tag("span")
                span.string = token
                # A small/immaterial value — a list marker, a year, or a
                # sub-100 count — recurs all over the document, so finding the
                # digit "somewhere" in the PDF is not validation.  Never stamp
                # such a number green (that reads as "verified correct" when it
                # is not — e.g. an HTML enumerator "1" whose PDF marker is
                # "a)"): render it neutral and leave it out of the verified
                # tally.  Only genuinely absent values still flag.
                if ok and _is_minor(text, start, end, key, token):
                    span["class"] = "secv-num-minor"
                    span["title"] = (
                        "Small/immaterial value (below 100, a year, or a list "
                        "marker). Present in the PDF but not independently "
                        "verified — check by eye if it matters."
                    )
                    fragments.append(span)
                    cursor = end
                    replaced = True
                    continue
                self.result.figures_total += 1
                if ok:
                    self.result.figures_ok += 1
                    pages = self.corpus.pages_for_number(key)
                    span["class"] = "secv-num-ok"
                    span["title"] = "Validated: found in PDF at " + ", ".join(
                        self.corpus.page_label(p) for p in pages[:6]
                    )
                    fragments.append(span)
                else:
                    closest = self.corpus.closest_numbers(key)
                    remark = (
                        f"Figure “{token.strip()}” does not appear anywhere in the "
                        f"PDF (compared as {key}, sign-insensitive). "
                    )
                    if closest:
                        remark += "Closest figures in the PDF: " + "; ".join(closest) + ". "
                    remark += "Check for a typo, a transposed digit, or a stale value."
                    issue = self._new_issue("figure", "error", token, remark)
                    span["class"] = "secv-num-bad"
                    span["id"] = issue.anchor
                    span["title"] = f"#{issue.num}: {remark}"
                    span["data-secv-remark"] = remark
                    marker = soup.new_tag("a", href="#secv-summary")
                    marker["class"] = "secv-marker"
                    marker.string = f"[{issue.num}]"
                    fragments.append(span)
                    fragments.append(marker)
                    fragments.append(Comment(f" SECVERIFY REMARK #{issue.num}: {remark} "))
                    replaced = True
                cursor = end
                replaced = True
            if cursor < len(text):
                fragments.append(text[cursor:])
            if replaced:
                anchor_node = node
                for frag in fragments:
                    if isinstance(frag, str) and not isinstance(frag, NavigableString):
                        frag = NavigableString(frag)
                    anchor_node.insert_after(frag)
                    anchor_node = frag
                node.extract()

        # Reverse checks on significant PDF figures: never used in the HTML,
        # or used fewer times than the PDF uses them (one instance dropped).
        for key, count in sorted(
            self.corpus.number_counts.items(),
            key=lambda kv: -len(kv[0]),
        ):
            token = self.corpus.number_sample.get(key, key)
            if not is_significant(key, token):
                continue
            if key not in html_number_keys:
                self.result.pdf_figures_missing.append(
                    {
                        "figure": token,
                        "value": key,
                        "pdf_pages": [
                            self.corpus.page_label(p)
                            for p in self.corpus.pages_for_number(key)
                        ],
                        "occurrences": count,
                    }
                )
                continue
            html_count = self.html_corpus.number_counts.get(key, 0)
            if 0 < html_count < count:
                self.result.figure_count_mismatches.append(
                    {
                        "figure": token,
                        "value": key,
                        "pdf_count": count,
                        "html_count": html_count,
                        "pdf_pages": [
                            self.corpus.page_label(p)
                            for p in self.corpus.pages_for_number(key)
                        ],
                    }
                )

    def _run_phase1(self) -> None:
        from .phase1 import check_period_dates

        pdf_text = "\n".join(self.corpus.pages_raw)
        html_text = self.html_corpus.visible_text
        add = lambda kind, sev, exc, rem: self._new_issue(kind, sev, exc, rem)  # noqa: E731
        check_period_dates(pdf_text, html_text, add)
        # NOTE: identifier↔name association (B10) is implemented in phase1 but
        # NOT enabled — these filings' signature blocks are COLUMNAR (names and
        # DINs on different lines), so text-proximity binding gives a different
        # (wrong) name on the PDF side than the inline HTML and would false-
        # flag correct DINs.  Reliable binding needs the geometry engine;
        # deferred rather than ship a false-positive-prone check.

    def _run_phase2(self, clean_soup) -> None:
        from .grid import grid_compare

        for kind, sev, exc, rem in grid_compare(
            self.corpus, clean_soup, self.pdf_paths
        ):
            tier = "caution" if sev == "caution" else "act"
            self._new_issue(kind, sev, exc, rem, tier=tier)

    def _run_phase3(self, clean_soup) -> None:
        from .render import render_and_hidden_checks
        from .identity import check_identifier_geometry
        from .images import image_checks

        for kind, sev, exc, rem in render_and_hidden_checks(
            self.corpus, clean_soup, self.pdf_paths
        ):
            self._new_issue(kind, sev, exc, rem)
        # images: inventory + OCR of embedded ones — nothing image-borne silent
        for kind, sev, exc, rem in image_checks(
            self.corpus, clean_soup, self.pdf_paths
        ):
            self._new_issue(kind, sev, exc, rem)
        # Ordered number-sequence alignment: verifies each number by its
        # POSITION in the document's number sequence rather than by whether the
        # value exists somewhere.  This is what makes small values (list
        # markers, note references, bare counts) checkable at all — see
        # seqdiff.py.
        from .seqdiff import sequence_findings

        for kind, sev, exc, rem in sequence_findings(
            self.corpus.pages_raw, clean_soup
        ):
            self._new_issue(kind, sev, exc, rem)
        from .style import style_checks
        for kind, sev, exc, rem in style_checks(self.corpus, clean_soup, self.pdf_paths):
            issue = self._new_issue(kind, sev, exc, rem)
            el = self._match_block(canonical(exc, letters_only=True), 0.5)
            if el is not None:
                cls = el.get("class", []); cls = cls.split() if isinstance(cls, str) else cls
                el["class"] = cls + ["secv-case-warn" if kind == "case" else "secv-bold-warn"]
                el["title"] = f"#{issue.num}: {rem}"[:500]
        # geometry-bound identifier↔name association (B10) — see identity.py
        add = lambda kind, sev, exc, rem: self._new_issue(kind, sev, exc, rem)  # noqa: E731
        check_identifier_geometry(
            self.pdf_paths, self.html_corpus.visible_text, add
        )

    def _sign_census(self) -> None:
        """Document-wide sign check, independent of row-label length.

        A figure's sign is stripped by text canonicalisation and only
        verified inside the (gated) row check, so a negative shown as
        positive on a short-label row — e.g. ``Total equity 84,643`` →
        ``(84,643)`` — passes every other check.  Here the number of
        NEGATIVE occurrences of each significant magnitude is compared
        between the PDF and the HTML: any difference means a sign was added
        or dropped somewhere, whatever the row.
        """
        keys = set(self.corpus.neg_counts) | set(self.html_corpus.neg_counts)
        for key in sorted(keys):
            token = self.corpus.number_sample.get(key, key)
            if not is_significant(key, token):
                continue
            pdf_neg = self.corpus.neg_counts.get(key, 0)
            html_neg = self.html_corpus.neg_counts.get(key, 0)
            if html_neg == pdf_neg:
                continue
            if key not in self.html_corpus.number_keys:
                continue  # absent entirely — reported by the presence check
            pages = ", ".join(
                self.corpus.page_label(p)
                for p in self.corpus.pages_for_number(key)[:5]
            )
            if html_neg < pdf_neg:
                what = (
                    f"is negative {pdf_neg}× in the PDF but only {html_neg}× in "
                    "the HTML — a negative (parentheses/minus) appears to have "
                    "been dropped, turning a deduction into an addition"
                )
            else:
                what = (
                    f"is negative {pdf_neg}× in the PDF but {html_neg}× in the "
                    "HTML — a negative has been added that the PDF does not show"
                )
            self._new_issue(
                "sign",
                "error",
                token,
                f"Sign — the figure {token.strip()} {what}. In the PDF it "
                f"appears on {pages}. Locate every {token.strip()} in the HTML "
                "and confirm its sign matches the PDF.",
            )

    def _symbol_census(self) -> None:
        """Document-wide ``%`` and currency check, independent of row labels.

        Canonicalisation strips ``%``, ``₹`` and ``$`` so magnitudes still match
        across formatting differences.  Those attributes were then only compared
        *within* the (gated) row check, which meant a symbol change in PROSE
        passed everything: an adversarial audit confirmed that ``2.6% QoQ`` →
        ``2.6 QoQ`` and ``$3.8 Billion`` → ``₹3.8 Billion`` both stayed green.
        Both change the meaning completely — a growth rate becomes a bare count,
        a dollar amount becomes a rupee amount.

        This mirrors :meth:`_sign_census`: for every significant magnitude the
        number of ``%``-bearing occurrences, and of each currency symbol, is
        reconciled between the PDF and the HTML.  Any difference means a symbol
        was added or dropped somewhere, whatever row or sentence it sits in.
        """
        # NOTE on "%": a COUNT census is the wrong instrument and is not used
        # here.  Counting "%"-bearing occurrences per magnitude is dominated by
        # content-PRESENCE differences rather than symbol changes: on a real
        # press release every headline metric came up short because the HTML
        # renders that panel as a GIF, so the census re-reported four
        # already-known image-borne figures as fresh RED errors -- and keyed on
        # magnitude alone it also conflated "3.8%" with "$3.8".  Percent is
        # instead compared per ALIGNED OCCURRENCE in seqdiff, where the sequence
        # diff has already paired the PDF's token with the HTML's, so a genuine
        # symbol change is distinguishable from a merely missing occurrence.

        # Currency is compared as a SWAP only, never as a drop.  A filing
        # legitimately states the unit once in a column header and leaves the
        # cells bare, so "₹34.75" in the PDF against "34.75" in the HTML is
        # correct and must not be flagged.  What cannot be right is the HTML
        # using a symbol the PDF never uses for that magnitude — ₹ where the
        # source says $ — which changes the amount while every digit matches.
        pdf_syms: dict[str, set[str]] = {}
        html_syms: dict[str, set[str]] = {}
        for (key, sym), n in self.corpus.cur_counts.items():
            if n:
                pdf_syms.setdefault(key, set()).add(sym)
        for (key, sym), n in self.html_corpus.cur_counts.items():
            if n:
                html_syms.setdefault(key, set()).add(sym)
        for key in sorted(html_syms):
            token = self.corpus.number_sample.get(key, key)
            if not is_significant(key, token):
                continue
            if key not in self.html_corpus.number_keys:
                continue
            added = html_syms[key] - pdf_syms.get(key, set())
            if not added:
                continue
            was = ", ".join(sorted(pdf_syms.get(key, set()))) or "no symbol"
            self._new_issue(
                "currency",
                "error",
                token,
                f"Currency — the HTML shows {key} with “{', '.join(sorted(added))}” "
                f"but the PDF shows that figure with {was}. A swapped currency "
                "symbol leaves every digit unchanged, so the value checks stay "
                f"silent while the amount means something different. Confirm each "
                f"{key} in the HTML carries the same symbol as the PDF.",
            )

    def _identifier_census(self) -> None:
        """Verify statutory identifiers match between the PDF(s) and HTML.

        DIN, UDIN, membership, firm-registration, PAN and CIN numbers are
        excluded from the monetary-figure checks (they are not amounts), so a
        wrong one would otherwise slip through.  Each is keyword-anchored, so
        this is a targeted exact-match check: every identifier value in the
        HTML must appear against the same keyword in the PDF, and vice versa.
        """
        ident_re = re.compile(
            r"\b(DIN|UDIN|Membership\s+No|Firm'?s?\s+Registration\s+No|"
            r"Registration\s+No|PAN|CIN)\b\s*[:.]?\s*"
            r"(?=[A-Za-z0-9/\-]*\d)([A-Za-z0-9][A-Za-z0-9/\-]{3,})",
            re.I,
        )

        def collect(text: str) -> dict[str, set[str]]:
            out: dict[str, set[str]] = {}
            for m in ident_re.finditer(text):
                kw = re.sub(r"\s+", " ", m.group(1)).upper().replace("FIRMS", "FIRM'S")
                kw = kw.split(" NO")[0]  # DIN/UDIN/MEMBERSHIP/REGISTRATION/PAN/CIN
                val = re.sub(r"[^A-Za-z0-9]", "", m.group(2)).upper()
                out.setdefault(kw, set()).add(val)
            return out

        pdf_ids = collect("\n".join(self.corpus.pages_raw))
        html_ids = collect(self.html_corpus.visible_text)
        pdf_all = {v for vs in pdf_ids.values() for v in vs}
        for kw, vals in html_ids.items():
            for val in sorted(vals):
                # Present if exactly in the PDF, or one is a true prefix/
                # suffix of the other with ≥8 shared chars (an extraction
                # split) — NOT an arbitrary substring (a short membership
                # number sits inside a long UDIN, which must not vouch for it).
                if val in pdf_all or any(
                    min(len(val), len(p)) >= 8
                    and (
                        val.startswith(p)
                        or val.endswith(p)
                        or p.startswith(val)
                        or p.endswith(val)
                    )
                    for p in pdf_all
                ):
                    continue
                self._new_issue(
                    "identifier",
                    "review",
                    f"{kw} {val}",
                    f"Identifier to verify — “{kw} {val}” appears in the HTML "
                    "but not in the PDF(s) provided. Either the PDF does not "
                    "print this identifier (common for director DINs — then "
                    "confirm it against its source) or it is a transposed / "
                    "stale number. Statutory identifiers must be exact.",
                )

    def _recolor_located_tokens(self, soup, root) -> None:
        """Paint the specific token of a located census finding with its own
        severity colour, so a flagged identifier no longer hides inside a
        green (text-matched) block.

        The base passes colour by *presence* — a statutory identifier is not a
        monetary figure, so it is left uncoloured and its surrounding block is
        painted green when the words match.  Here each ``identifier`` /
        ``identifier-name`` finding's value is found in the body and wrapped in
        an amber (review) or red (error) span with its ``[n]`` marker.

        Symbol findings are painted the same way, and for the same reason.  A
        dropped ``%`` or a swapped currency leaves the MAGNITUDE correct, so the
        number passes the presence check and is stamped green — the finding would
        otherwise live only in the summary panel while the figure a reviewer
        looks at still reads as verified.  An adversarial audit caught exactly
        that: the ``percent`` issue fired, yet the changed value stayed green.
        """
        _WHAT = {
            "identifier": "identifier to verify",
            "identifier-name": "identifier to verify",
            "percent": "percent sign differs from the PDF",
            "currency": "currency symbol differs from the PDF",
        }
        targets: list[tuple[str, str, int, str]] = []
        for issue in self.result.issues:
            if issue.kind == "identifier":
                val = issue.excerpt.split()[-1]
            elif issue.kind == "identifier-name":
                val = issue.excerpt.split()[0]
            elif issue.kind in ("percent", "currency"):
                # excerpt starts with the magnitude ("2.6: PDF has % / …")
                val = issue.excerpt.split(":")[0].strip()
            else:
                continue
            if len(val) >= 3:
                targets.append((val, issue.severity, issue.num, _WHAT[issue.kind]))

        for val, severity, num, what in targets:
            cls = "secv-token-bad" if severity == "error" else "secv-token-warn"
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(val)}(?![A-Za-z0-9])")
            marked = False
            for node in list(root.find_all(string=True)):
                if not isinstance(node, NavigableString) or isinstance(node, Comment):
                    continue
                if node.find_parent(SKIP_PARENTS) is not None:
                    continue
                if node.find_parent(
                    class_=["secv-marker", "secv-token-warn", "secv-token-bad"]
                ) is not None:
                    continue
                s = str(node)
                if not pattern.search(s):
                    continue
                parts, last = [], 0
                for m in pattern.finditer(s):
                    if m.start() > last:
                        parts.append(soup.new_string(s[last:m.start()]))
                    span = soup.new_tag("span", **{"class": cls})
                    span.string = m.group(0)
                    span["title"] = f"#{num}: {what}"
                    parts.append(span)
                    if not marked:  # one navigable marker per finding is enough
                        marker = soup.new_tag("a", href="#secv-summary")
                        marker["class"] = "secv-marker"
                        marker.string = f"[{num}]"
                        parts.append(marker)
                        marked = True
                    last = m.end()
                if last < len(s):
                    parts.append(soup.new_string(s[last:]))
                node.replace_with(*parts)

    def _recolor_grid_cells(self, soup, root) -> None:
        """Paint the figures of a grid-flagged table row with the finding's
        colour (red for a value/order error, brown for a wide-matrix caution),
        so a row whose cells are each individually present-green no longer
        looks validated despite being flagged."""
        from .grid import cells_label_figs
        from .reviewzones import _clean_cell_text

        findings: dict[tuple, tuple[str, int]] = {}
        for issue in self.result.issues:
            if not issue.kind.startswith("grid") or ": HTML " not in issue.excerpt:
                continue
            hlbl = issue.excerpt.split(": HTML ")[0]
            hfigs = tuple(
                issue.excerpt.split(": HTML ")[1].split(" / PDF ")[0].split()
            )
            findings[(hlbl, hfigs)] = (issue.severity, issue.num)
        if not findings:
            return

        for tr in root.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) < 2:
                continue
            lbl, figs = cells_label_figs([_clean_cell_text(c) for c in cells])
            spans = []
            for cell in cells:
                spans.extend(cell.find_all("span", class_="secv-num-ok"))
            key = (lbl, tuple(figs))
            if key not in findings:
                continue
            severity, num = findings[key]
            cls = "secv-token-bad" if severity == "error" else "secv-token-caution"
            for span in spans:
                classes = span.get("class", [])
                if isinstance(classes, str):
                    classes = classes.split()
                span["class"] = [c for c in classes if c != "secv-num-ok"] + [cls]
                span["title"] = f"#{num}: table row flagged — verify this row"
            if spans:
                marker = soup.new_tag("a", href="#secv-summary")
                marker["class"] = "secv-marker"
                marker.string = f"[{num}]"
                spans[-1].insert_after(marker)

    # -- text ------------------------------------------------------------
    def _date_mismatch(
        self, sentence: str, letters: str
    ) -> tuple[str, str, str] | None:
        """Compare calendar dates when the words matched but the digits didn't.

        The sentence's words are present verbatim in the PDF (letters-only
        match), so we align to that letter span, read the PDF's original
        wording there, and compare the dates in order.  Flagged only when
        both sides carry the SAME number of dates but a value differs — an
        unequal count means a fractured/interleaved PDF date, which is left
        to the other checks so this guard never false-alarms on extraction
        artifacts.
        """
        html_dates = _dates_in(sentence)
        if not html_dates:
            return None
        view = self.corpus.letters
        pos = view.canon.find(letters)
        if pos < 0 or pos >= len(view.index_map):
            return None
        # Read the PDF's original wording aligned to this letter run.  The
        # raw window is bounded to roughly the sentence's own length (plus a
        # little slack for the final date's trailing digits, which the
        # letters-canon drops) so it never reaches into the next sentence —
        # and if a table interleaves figures and lengthens the PDF line, the
        # window simply truncates and the count guard below stays silent.
        page_idx, off = view.index_map[pos]
        raw = self.corpus.pages_raw[page_idx]
        window = raw[off : off + len(sentence) + 12]
        pdf_dates = _dates_in(window)
        if len(pdf_dates) != len(html_dates):
            return None
        diffs = [(h, p) for h, p in zip(html_dates, pdf_dates) if h != p]
        if not diffs:
            return None
        h, p = diffs[0]
        more = f" (and {len(diffs) - 1} more date(s) differ)" if len(diffs) > 1 else ""
        return "error", (
            "Date mismatch — the wording matches the PDF but a reporting date "
            f"differs. The HTML reads “{h}” where the PDF reads “{p}”{more}. A "
            "wrong period or comparative date mis-states the whole column; "
            "verify against the PDF."
        ), "discrepancy"

    def _check_verbatim_block_order(self, doc_text: str) -> None:
        """Catch a verbatim block placed where a near-identical one belongs.

        Two sentences with the SAME words but DIFFERENT figures (a note copied
        for two periods — "… year ended March 31 2026 … 34,764 …" vs "… 2025 …
        31,998 …") both match the PDF verbatim, so each is stamped green; if
        they are SWAPPED, nothing else notices because every word and figure
        still exists.  Group the HTML's sentences by their words alone; within
        a group whose members differ only in figures and each occur once in
        the PDF, the HTML order must match the PDF order — any inversion is a
        block sitting in the wrong period's place.  (Measured zero false
        positives on the reference filings: only same-word/different-figure
        pairs are compared, never the document's ordinary reordered prose.)
        """
        from collections import defaultdict

        from .coverage import _lis_indices

        pdfc = self.corpus.alnum.canon
        groups: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for idx, sent in enumerate(split_sentences(doc_text)):
            letters = canonical(sent, letters_only=True)
            if len(letters) < 40:
                continue
            groups[letters].append((idx, canonical(sent), sent))
        for items in groups.values():
            if len({a for _i, a, _s in items}) < 2:
                continue  # all identical → not a figures-only variation
            located = []
            for _i, alnum, sent in items:
                p = pdfc.find(alnum)
                if p >= 0 and pdfc.find(alnum, p + 1) == -1:
                    located.append((p, sent))  # unique in the PDF
            if len(located) < 2:
                continue
            keep = set(_lis_indices([p for p, _s in located]))
            for j, (p, sent) in enumerate(located):
                if j in keep:
                    continue
                self._new_issue(
                    "block-order",
                    "error",
                    _shorten(sent),
                    "Block out of place — this sentence matches the PDF "
                    "word-for-word, but a near-identical sentence that differs "
                    "only in its figures sits in the PDF at this position "
                    "instead. Two period versions of a note may have been "
                    "swapped, so the figures here belong to the wrong period. "
                    f"Verify placement: “{_shorten(sent)}”.",
                )

    def _check_sentence(self, sentence: str) -> tuple[str, str, str]:
        """Return ``(status, remark, category)``.

        status ∈ ok / review / error / skip; category classifies non-ok
        findings for triage: "discrepancy" (both texts exist and differ),
        "absent" (no counterpart in the PDF at all), or "layout" (words
        verified on one PDF page, print order differs).
        """
        canon = canonical(sentence)
        if not canon or canon.isdigit():
            return "skip", "", ""
        # Tier 1: exact match including figures.
        if canon in self.corpus.alnum.canon:
            return "ok", "", ""
        # Tier 2: exact match of the words alone.  PDF extraction interleaves
        # table figures/headers into label text unpredictably; the figures
        # themselves are validated by the separate number pass.
        letters = canonical(sentence, letters_only=True)
        if letters and letters in self.corpus.letters.canon:
            # Words match verbatim, but canonicalisation dropped the digits.
            # A reporting date carried inside identical wording ("quarter
            # ended June 30, 2025" where the PDF reads "…2026") would slip
            # through as green.  Align to the PDF at this letter span and
            # compare the dates positionally.
            date_issue = self._date_mismatch(sentence, letters)
            if date_issue is not None:
                return date_issue
            return "ok", "", ""
        needle = letters or canon
        view = self.corpus.letters if letters else self.corpus.alnum
        # Before trusting a fuzzy match — which may land on a *similar
        # sentence elsewhere in the document* — look for the sentence as a
        # scrambled table row: all of its words, in order, within a tight
        # window on some page (the PDF layout interleaves other columns'
        # words between them).  The tightest such window is where the
        # content actually lives.
        words = [
            canonical(w, letters_only=True)
            for w in re.findall(r"[^\W\d_]+", sentence)
        ]
        words = [w for w in words if len(w) >= 2]
        if len(words) >= 3:
            hit = self._find_interleaved_page(words, len(needle))
            if hit is not None:
                page_no, local_start, local_end = hit
                g_start = self.corpus.letters.page_starts[page_no - 1] + local_start
                g_end = self.corpus.letters.page_starts[page_no - 1] + local_end
                label = self.corpus.page_label(page_no)
                snippet = self.corpus.raw_snippet(
                    self.corpus.letters, g_start, g_end
                )
                return "review", (
                    f"“{_shorten(sentence)}” is present on PDF {label}, but the "
                    "PDF layout interleaves other table columns' text between "
                    f"its words (the PDF reads: “{snippet}”). The wording "
                    "itself matches in order — a quick glance suffices."
                ), "layout"
        match = find_best_match(view.canon, needle)
        if match is None:
            coverage = self._word_coverage(sentence)
            if coverage is not None:
                page, pct = coverage
                return "review", (
                    f"“{_shorten(sentence)}” is not contiguous in the PDF, but "
                    f"{pct:.0%} of its words appear together on PDF {page} — "
                    "typically a multi-column table header that wraps onto "
                    "several lines in the PDF. Verify that page manually."
                ), "layout"
            if len(needle) < 12:
                # Too short to fuzzy-match, so only an exact lookup was
                # possible — and it failed.  A lone short label like a table
                # header ("Particulars", "Opinion") is frequently rendered by
                # the PDF as a styled/merged/coloured header cell, or sits on
                # a scanned page, that text extraction cannot read; declaring
                # it "absent" (red) is then a false alarm.  It is genuinely
                # unverifiable, so surface it for the eye (review) rather than
                # assert it is missing — and never pass it as green.
                return "review", (
                    f"“{sentence.strip()}” could not be located in the PDF's "
                    "text. Short headings/labels are often rendered as styled "
                    "or merged header cells (or lie on scanned pages) that "
                    "text extraction cannot read — confirm by eye that it "
                    "appears in the PDF."
                ), "layout"
            return "error", (
                f"Not found in the PDF: “{_shorten(sentence)}”. "
                "No similar passage exists — this content may be missing from "
                "or added relative to the PDF."
            ), "absent"
        start, end, ratio = match
        page = self.corpus.page_label(view.page_of(start))
        snippet = self.corpus.raw_snippet(view, start, end)
        # Layout takes precedence over a fuzzy "discrepancy": if EVERY word of
        # the sentence is present together on some PDF page, the wording is
        # there — only the table arrangement differs (e.g. "Manikantha A.G.S."
        # vs "A.G.S. Manikantha").  A genuine wording change (a word actually
        # absent, like "consolidated" vs "standalone") fails word-coverage and
        # correctly falls through to a discrepancy.
        coverage = self._word_coverage(sentence)
        if coverage is not None:
            cov_page, pct = coverage
            return "review", (
                f"“{_shorten(sentence)}” is present on PDF {cov_page} (all its "
                "words appear there) but the table arrangement/order differs — "
                "a layout artifact; a quick glance confirms it."
            ), "layout"
        unit_swap = _scale_unit_swap(sentence, snippet)
        if unit_swap is not None:
            hu, pu = unit_swap
            return "error", (
                f"Unit of scale — this matches the PDF except for the reporting "
                f"unit: the HTML says “{hu}” where the PDF says “{pu}”. Every "
                "figure under this heading would be mis-scaled by orders of "
                f"magnitude. HTML: “{_shorten(sentence)}”; PDF {page}: “{snippet}”."
            ), "discrepancy"
        if ratio >= FUZZY_REVIEW_RATIO:
            return "review", (
                f"Close but not identical to the PDF (similarity {ratio:.0%}). "
                f"HTML says: “{_shorten(sentence)}”. "
                f"PDF {page} says: “{snippet}”. Reconcile the wording/figures."
            ), "discrepancy"
        return "error", (
            f"Does not match the PDF. HTML says: “{_shorten(sentence)}”. "
            f"The nearest passage (PDF {page}, similarity {ratio:.0%}) is: "
            f"“{snippet}”."
        ), "discrepancy"

    def _find_interleaved_page(
        self, words: list[str], needle_len: int
    ) -> tuple[int, int, int] | None:
        """Page holding *words* in order within a tight window, or None.

        Returns ``(page_no, local_start, local_end)`` in that page's
        letters-canonical coordinates.  The window bound keeps this honest:
        the words must sit close together (a table row with interleaved
        column text), not merely be scattered across the page.
        """
        bound = 3 * needle_len + 60
        best: tuple[int, int, int, int] | None = None  # (size, page, s, e)
        for page_idx, page_canon in enumerate(self.corpus.page_letters):
            if not page_canon or any(w not in page_canon for w in words):
                continue
            start = 0
            while True:
                span = word_subsequence_span(page_canon, words, start)
                if span is None:
                    break
                size = span[1] - span[0]
                if size <= bound and (best is None or size < best[0]):
                    best = (size, page_idx + 1, span[0], span[1])
                start = span[0] + 1
        if best is None:
            return None
        return (best[1], best[2], best[3])

    def _word_coverage(self, sentence: str) -> tuple[str, float] | None:
        """Best single PDF page containing (almost) every word of *sentence*.

        Returns ``(page, coverage)`` when some page contains ≥85% of the
        words; used to downgrade table headers that the PDF wraps across
        lines (words present, order scrambled) from error to review.
        """
        words = [
            canonical(w, letters_only=True)
            for w in re.findall(r"[^\W\d_]+", sentence)
        ]
        words = [w for w in words if w]
        # Need enough signal: several words, or a name-like word plus initials.
        if len(words) < 2 or sum(len(w) for w in words) < 8:
            return None
    # noqa: kept simple — pages are few and short
        candidates: list[tuple[int, float]] = []
        for page_idx, page_canon in enumerate(self.corpus.page_letters):
            if not page_canon:
                continue
            hit = sum(1 for w in words if w in page_canon)
            pct = hit / len(words)
            if pct >= 0.85:
                candidates.append((page_idx + 1, pct))
        if not candidates:
            return None
        best_pct = max(pct for _p, pct in candidates)
        if best_pct < 0.999:
            # An unmatched word may be a genuine wording change: not layout.
            return None
        candidates = [c for c in candidates if c[1] == best_pct]
        # Among full-coverage pages, the one where the words cluster into the
        # tightest in-order window is where the content actually lives.
        def window_size(page_no: int) -> int:
            size = smallest_subsequence_window(
                self.corpus.page_letters[page_no - 1], words
            )
            return size if size is not None else 10**9

        page_no = min(candidates, key=lambda c: window_size(c[0]))[0]
        return (self.corpus.page_label(page_no), best_pct)

    def _annotate_blocks(self, soup: BeautifulSoup, root) -> None:
        for block in root.find_all(BLOCK_TAGS):
            if block.find(BLOCK_TAGS) is not None:
                continue  # only leaf blocks: finest granularity
            if block.find_parent(SKIP_PARENTS) is not None:
                continue
            text = block.get_text(" ", strip=True)
            canon = canonical(text)
            if not canon:
                continue
            if canon.isdigit():
                continue  # pure figures: covered by the number pass

            self.result.text_blocks_total += 1
            worst = "ok"
            remarks: list[str] = []
            cats: set[str] = set()
            for sentence in split_sentences(text):
                status, remark, cat = self._check_sentence(sentence)
                if status == "skip":
                    continue
                if status == "review" and worst == "ok":
                    worst = "review"
                if status == "error":
                    worst = "error"
                if remark:
                    remarks.append(remark)
                if cat:
                    cats.add(cat)

            classes = block.get("class", [])
            if isinstance(classes, str):
                classes = [classes]
            if worst == "ok":
                self.result.text_blocks_ok += 1
                classes.append("secv-text-ok")
                block["class"] = classes
                continue

            severity = "review" if worst == "review" else "error"
            if worst == "review":
                self.result.text_blocks_review += 1
            remark = " || ".join(remarks)
            # A concrete discrepancy outranks absent content, which outranks
            # a layout artifact, when a block mixes several finding types.
            if "discrepancy" in cats:
                tier = "act"
            elif "absent" in cats:
                tier = "absent"
            else:
                tier = "layout"
            issue = self._new_issue("text", severity, text, remark, tier=tier)
            classes.append("secv-text-warn" if worst == "review" else "secv-text-bad")
            block["class"] = classes
            block["id"] = block.get("id") or issue.anchor
            block["title"] = f"#{issue.num}: {remark}"[:1000]
            block["data-secv-remark"] = remark
            marker = soup.new_tag("a", href="#secv-summary")
            marker["class"] = "secv-marker"
            marker.string = f"[{issue.num}]"
            block.append(marker)
            block.append(Comment(f" SECVERIFY REMARK #{issue.num}: {remark} "))

    # -- entry point -------------------------------------------------------
    def run(self, html_text: str, pdf_name: str, html_name: str) -> Result:
        html_text = strip_edgar_submission_header(html_text)
        # Run on the RAW markup, before any parse: a conflicting pair of style
        # attributes is resolved differently by a browser and by a text
        # extractor, so the evidence is destroyed by parsing (see
        # render.duplicate_style_findings).
        from .render import duplicate_style_findings

        for kind, sev, exc, rem in duplicate_style_findings(html_text):
            self._new_issue(kind, sev, exc, rem)
        soup = BeautifulSoup(html_text, "html.parser")
        root = soup.body or soup
        # Snapshot the HTML's visible text before any highlighting is added,
        # then verify the reverse direction: every PDF line must be reflected.
        # Per-leaf-block texts feed boundary-respecting occurrence counts.
        self._block_index = _collect_block_index(root)
        block_texts = [b["text"] for b in self._block_index]
        self.html_corpus = HtmlCorpus(root.get_text(" "), block_texts)
        self.result.coverage = check_pdf_coverage(
            self.corpus.pages_raw,
            self.html_corpus,
            self.corpus.alnum.canon,
            self.corpus.letters.canon,
            self.corpus.page_labels,
        )
        self.result.coverage.low_text_pages = list(self.corpus.low_text_pages)
        self._annotate_blocks(soup, root)
        self._annotate_numbers(soup, root)
        self._sign_census()
        self._symbol_census()
        self._identifier_census()
        self._check_verbatim_block_order(root.get_text(" "))
        if self._at_least("alpha"):
            self._run_phase1()
        # Phases 2/3 parse a pristine copy of the HTML: the number/block
        # annotation above injects "[n]" summary markers into cells, which
        # would otherwise pollute table-cell figure extraction.
        if self._at_least("beta") or self._at_least("sigma"):
            clean_soup = BeautifulSoup(html_text, "html.parser")
            if self._at_least("beta"):
                self._run_phase2(clean_soup)
            if self._at_least("sigma"):
                self._run_phase3(clean_soup)
        cov_anchors = self._register_coverage_issues()
        unplaced = self._insert_inline_omissions(soup, cov_anchors)
        self._recolor_located_tokens(soup, root)
        self._recolor_grid_cells(soup, root)
        if self.review_zones:
            from .reviewzones import mark_review_zones

            self._zone_counts = mark_review_zones(
                soup, root, self.corpus, strict=self.strict, footed=self.footed
            )
        self._pairing_sanity()
        _inject_banner(
            soup, root, self.result, pdf_name, html_name, unplaced,
            zone_counts=self._zone_counts if self.review_zones else None,
            strict=self.strict,
        )
        self.result.html_out = str(soup)
        return self.result

    #: below this share of figures validating, the PDF and the HTML are almost
    #: certainly not the same document — a real pair lands well above 95%
    PAIRING_MIN_FIGURES = 0.70
    #: …corroborated by text blocks, so a genuinely bad conversion (which fails
    #: on figures but still matches its wording) is not mistaken for a mispair
    PAIRING_MIN_BLOCKS = 0.70

    def _pairing_sanity(self) -> None:
        """Warn loudly when the PDF and the HTML are not the same document.

        Two files can be the right *kind* and still be the wrong pair — a
        quarter's exhibit against the previous quarter's PDF, which is easy to do
        when successive downloads are both called ``IFRS_USD_PR.pdf``.  Every
        check then reports against a source that never contained these figures,
        so the run fills with hundreds of findings that say nothing about the
        filing's accuracy.  Worse, it reads as though the TOOL failed.

        Both ratios must be low before this fires.  A genuinely bad conversion
        breaks figures while its wording still matches the source, so requiring
        the text blocks to fail as well distinguishes "wrong document" from
        "right document, badly converted" — the latter must stay a normal run
        with normal findings.
        """
        figs_total = self.result.figures_total
        blocks_total = self.result.text_blocks_total
        if figs_total < 30 or blocks_total < 20:
            return  # too small to judge
        fig_ratio = self.result.figures_ok / figs_total
        blk_ratio = self.result.text_blocks_ok / blocks_total
        if fig_ratio >= self.PAIRING_MIN_FIGURES or blk_ratio >= self.PAIRING_MIN_BLOCKS:
            return
        self._new_issue(
            "pairing",
            "error",
            f"only {self.result.figures_ok}/{figs_total} figures and "
            f"{self.result.text_blocks_ok}/{blocks_total} text blocks match",
            "WRONG PDF FOR THIS EXHIBIT — read this before anything else. Only "
            f"{fig_ratio:.0%} of the HTML's figures and {blk_ratio:.0%} of its "
            "text blocks were found in the reference PDF(s). A correct pair "
            "matches well above 95%. Almost certainly the PDF is not the source "
            "of this exhibit — most often a different period's file (successive "
            "downloads are frequently identically named), or the exhibit also "
            "contains an auditor's report whose PDF was not supplied. Every "
            "other finding in this run is measured against the wrong source and "
            "should be ignored until the pairing is fixed.",
        )

    def _insert_inline_omissions(
        self, soup: BeautifulSoup, anchors: dict[int, str]
    ) -> list[tuple[int, "CoverageLine"]]:
        """Place each omitted PDF line as a callout where it belongs.

        For every missing/probably-dropped PDF line, the nearest *preceding*
        PDF line that IS reflected in the HTML is located in the DOM, and a
        red/amber callout box is inserted right after it — so the omission
        shows up at the exact position in the document where the content
        should have been.  Lines that cannot be anchored are returned so the
        summary panel can list them instead.
        """
        unplaced: list[tuple[int, CoverageLine]] = []
        lines = self.result.coverage.lines
        for idx, anchor_id in anchors.items():
            line = lines[idx]
            block = self._find_anchor_block(idx)
            if block is None:
                unplaced.append((idx, line))
                continue
            severity = "bad" if line.status == "missing" else "warn"
            heading = {
                "sign": "SIGN ERROR",
                "column-order": "COLUMN ORDER",
                "currency": "CURRENCY MISMATCH",
                "percent": "PERCENT MISMATCH",
                "row-value": "WRONG FIGURE IN ROW",
                "duplicate": "DUPLICATED IN HTML",
                "unit-scale": "UNIT OF SCALE",
            }.get(
                line.issue_kind,
                "MISSING FROM HTML"
                if line.status == "missing"
                else "CHECK — POSSIBLY DROPPED",
            )
            text = (
                f"⛔ {heading} · PDF {line.label or line.page}: “{line.text}” — "
                f"{line.remark}"
            )
            row = block.find_parent("tr")
            if row is not None:
                holder = soup.new_tag("tr")
                cell = soup.new_tag("td")
                cell["colspan"] = str(max(1, len(row.find_all(["td", "th"]))))
                holder.append(cell)
                target, insert_after = cell, row
            else:
                holder = cell = soup.new_tag("div")
                target, insert_after = holder, block
            cell["class"] = f"secv-callout secv-callout-{severity}"
            cell["id"] = anchor_id
            target.string = text
            insert_after.insert_after(holder)
            holder.insert_after(
                Comment(f" SECVERIFY OMISSION (PDF {line.label or line.page}): {line.remark} ")
            )
        return unplaced

    def _match_block(self, a_letters: str, frac: float):
        """Find the DOM block reflecting *a_letters*, or None."""
        if len(a_letters) < 10:
            return None
        # A block matching only a small piece of the line (a generic cell
        # like "Particulars" that exists in every statement) must not anchor
        # a callout — it would be planted at a guessed, likely wrong place.
        min_cover = max(10, len(a_letters) // 2)
        matches = [
            b
            for b in self._block_index
            if (b["letters"] and a_letters in b["letters"])
            or (len(b["letters"]) >= min_cover and b["letters"] in a_letters)
        ]
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]["el"]
        # Same content appears in several places (e.g. an index entry and its
        # section heading): pick the occurrence whose relative position in
        # the HTML best matches the line's position in the PDF.
        return min(
            matches, key=lambda b: abs(b["pos"] / len(self._block_index) - frac)
        )["el"]

    def _find_anchor_block(self, line_idx: int):
        """DOM element after which an omission callout should be placed."""
        lines = self.result.coverage.lines
        line = lines[line_idx]
        frac = line_idx / max(1, len(lines))
        # Try the line's own text first: count-shortfall lines and
        # row-integrity mismatches have their label present in the HTML —
        # the callout belongs right there.
        el = self._match_block(canonical(line.text, letters_only=True), frac)
        if el is not None:
            return el
        # Otherwise anchor after the nearest preceding covered PDF line.
        for back in range(1, 16):
            j = line_idx - back
            if j < 0:
                break
            prev = lines[j]
            if prev.status != "ok":
                continue
            el = self._match_block(canonical(prev.text, letters_only=True), frac)
            if el is not None:
                return el
        return None

    def _register_coverage_issues(self) -> dict[int, str]:
        """Turn missing PDF lines into numbered issues; map line idx→anchor."""
        anchors: dict[int, str] = {}
        for idx, line in enumerate(self.result.coverage.lines):
            if line.status == "missing":
                severity = "error"
            elif line.escalate:  # probable omission of repeated content
                severity = "review"
            else:
                continue
            issue = self._new_issue(
                line.issue_kind,
                severity,
                f"(PDF {line.label or line.page}) {line.text}",
                line.remark,
            )
            # The highlight for a coverage finding is an inline callout box
            # (or a summary-panel entry when it cannot be positioned).
            issue.anchor = f"secv-cov-{idx}"
            anchors[idx] = issue.anchor
        for dup in self.result.coverage.duplications:
            self._new_issue("duplicate", "review", dup.text[:160], dup.remark)
        for oi in self.result.coverage.order_issues:
            span = (
                f"“{_shorten(oi.first_text, 100)}”"
                if oi.count == 1
                else f"“{_shorten(oi.first_text, 80)}” … “{_shorten(oi.last_text, 80)}”"
                f" ({oi.count} lines)"
            )
            if oi.review:
                self._new_issue(
                    "order",
                    "review",
                    f"(PDF {oi.pdf_label}) {oi.first_text}",
                    f"Relocated content — this text is word-for-word correct "
                    f"but appears {oi.direction} in the HTML than the PDF "
                    f"order requires (by roughly a paragraph; it sits near the "
                    f"content of PDF {oi.near_label}). Affected: {span}. "
                    "Verify the paragraph sits under the intended heading.",
                )
            else:
                self._new_issue(
                    "order",
                    "error",
                    f"(PDF {oi.pdf_label}) {oi.first_text}",
                    f"Out of sequence: this content is on PDF {oi.pdf_label}, but "
                    f"in the HTML it appears {oi.direction} than the PDF order "
                    f"requires — it sits near the content of PDF {oi.near_label}. "
                    f"Affected: {span}. Check whether this section was moved "
                    "during conversion.",
                )
        for m in self.result.figure_count_mismatches:
            pages = ", ".join(str(p) for p in m["pdf_pages"][:5])
            self._new_issue(
                "figure-count",
                "review",
                m["figure"],
                f"Figure “{m['figure']}” appears {m['pdf_count']}× in the PDF "
                f"({pages}) but only {m['html_count']}× in the HTML — one "
                "occurrence may have been dropped. Check each place it should "
                "appear.",
            )
        return anchors


def _shorten(text: str, max_len: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _collect_block_index(root) -> list[dict]:
    """Snapshot every leaf block's canonical text (pre-annotation) for
    anchoring omission callouts to their document position."""
    index: list[dict] = []
    for block in root.find_all(BLOCK_TAGS):
        if block.find(BLOCK_TAGS) is not None:
            continue
        if block.find_parent(SKIP_PARENTS) is not None:
            continue
        text = block.get_text(" ", strip=True)
        if not canonical(text):
            continue
        index.append(
            {
                "el": block,
                "text": text,
                "letters": canonical(text, letters_only=True),
                "pos": len(index),
            }
        )
    return index


_CSS = """
.secv-num-ok { background: #52d05c; border-radius: 2px; padding: 0 1px; }
.secv-num-minor { background: #fff200; border-radius: 2px; padding: 0 1px; }
.secv-num-bad { background: #ff6b6b; outline: 2px solid #a00000; border-radius: 2px;
                font-weight: bold; padding: 0 1px; }
.secv-num-review { background: #7cb8ff; outline: 1px solid #1560c0; border-radius: 2px;
                   padding: 0 1px; }
.secv-token-warn { background: #ffd24d; outline: 2px solid #9a6a00; border-radius: 2px;
                   padding: 0 1px; font-weight: bold; }
.secv-token-bad { background: #ff9d9d; outline: 2px solid #a00000; border-radius: 2px;
                  padding: 0 1px; font-weight: bold; }
.secv-case-warn { outline: 3px solid #8a2be2 !important; }  /* violet = case differs */
.secv-bold-warn { outline: 3px solid #e83e8c !important; }  /* magenta = bold lost */
.secv-token-caution { background: #e0b483; outline: 2px solid #8a5a2b; border-radius: 2px;
                      padding: 0 1px; font-weight: bold; }
.secv-xref-review { background: #7cb8ff; outline: 1px solid #1560c0; border-radius: 2px;
                    padding: 0 1px; }
img.secv-img-review { outline: 2px dashed #1560c0; outline-offset: 1px; }
.secv-text-ok { background: #a4e8a0 !important; border-left: 5px solid #1e8a26 !important; }
.secv-text-warn { background: #ffd24d !important; outline: 2px solid #9a6a00;
                  border-left: 5px solid #9a6a00 !important; }
.secv-text-bad { background: #ff9d9d !important; outline: 2px solid #a00000;
                 border-left: 5px solid #a00000 !important; }
.secv-marker { color: #a00000; background: #fff; font-weight: bold; font-size: 8pt;
               vertical-align: super; text-decoration: none; padding: 0 2px; }
#secv-summary { font: normal 10pt Arial, Helvetica, sans-serif; border: 3px solid #333;
                background: #fafafa; padding: 12px 16px; margin: 0 0 18px 0; }
#secv-summary h2 { margin: 0 0 6px 0; font-size: 13pt; }
#secv-summary table { border-collapse: collapse; width: 100%; margin-top: 6px; }
#secv-summary th, #secv-summary td { border: 1px solid #999; padding: 4px 6px;
                                     text-align: left; font-size: 9pt; vertical-align: top; }
#secv-summary .sev-error { color: #c00000; font-weight: bold; }
#secv-summary .sev-review { color: #b8860b; font-weight: bold; }
#secv-summary .sev-caution { color: #8a5a2b; font-weight: bold; }
.secv-legend span { padding: 1px 6px; margin-right: 10px; }
#secv-assurance { border: 2px solid #1e8a26; background: #f0fbef; padding: 8px 12px;
                  margin: 8px 0; border-radius: 3px; }
#secv-assurance h3 { font-size: 11pt; color: #1e6b24; }
#secv-assurance ul { font-size: 9pt; }
#secv-assurance .secv-assure-warn { color: #a00000; font-weight: bold; background: #ffecec;
                                    padding: 4px 8px; border-radius: 3px; }
.secv-callout { font: bold 9pt Arial, Helvetica, sans-serif !important;
                padding: 6px 10px !important; margin: 4px 0; border-radius: 3px; }
.secv-callout-bad { background: #ff9d9d !important; border: 2px solid #a00000 !important; }
.secv-callout-warn { background: #ffd24d !important; border: 2px solid #9a6a00 !important; }
"""


def _inject_banner(
    soup,
    root,
    result: Result,
    pdf_name: str,
    html_name: str,
    unplaced: list[tuple[int, CoverageLine]] | None = None,
    zone_counts: tuple[int, int] | None = None,
    strict: bool = False,
) -> None:
    style = soup.new_tag("style")
    style.string = _CSS
    head = soup.head
    (head or root).insert(0, style)

    # Declare UTF-8 so browsers render typographic characters (non-breaking
    # spaces, em-dashes, curly quotes, ₹) correctly instead of mojibake like
    # "Â" — many SEC exhibit HTMLs ship without a charset meta.
    if head is not None and not head.find(
        "meta", attrs={"charset": True}
    ):
        meta = soup.new_tag("meta")
        meta["charset"] = "utf-8"
        head.insert(0, meta)

    ok_pct = (
        f"{result.figures_ok}/{result.figures_total}"
        if result.figures_total
        else "0/0"
    )
    def tier_rows(tier: str) -> str:
        rows = [
            f'<tr><td><a href="#{issue.anchor}">#{issue.num}</a></td>'
            f"<td>{issue.kind}</td>"
            f'<td class="sev-{issue.severity}">{issue.severity.upper()}</td>'
            f"<td>{html_mod.escape(issue.excerpt)}</td>"
            f"<td>{html_mod.escape(issue.remark)}</td></tr>"
            for issue in result.issues
            if issue.tier == tier
        ]
        if not rows:
            return ""
        return (
            "<table><tr><th>#</th><th>Type</th><th>Severity</th>"
            "<th>HTML content</th><th>Remark — what to correct</th></tr>"
            + "".join(rows)
            + "</table>"
        )

    act_n = sum(1 for i in result.issues if i.tier == "act")
    absent_n = sum(1 for i in result.issues if i.tier == "absent")
    layout_n = sum(1 for i in result.issues if i.tier == "layout")
    caution_n = sum(1 for i in result.issues if i.tier == "caution")

    issue_table = ""
    if act_n:
        issue_table += (
            f'<h4 class="sev-error" style="margin:8px 0 2px 0">🔴 Discrepancies — '
            f"act on these ({act_n})</h4>"
            "<p style='margin:2px 0'>Both documents carry this content but it "
            "differs (wrong figure, changed wording, or a dropped instance).</p>"
            + tier_rows("act")
        )
    if absent_n:
        issue_table += (
            f"<details><summary><b>Content with no counterpart in this PDF "
            f"({absent_n})</b> — e.g. the auditor's report or SEC-only labels; "
            "verify against their own source document (click to expand)"
            f"</summary>{tier_rows('absent')}</details>"
        )
    if caution_n:
        issue_table += (
            f"<details><summary><b>🟤 Possible issues — lower confidence "
            f"({caution_n})</b> — checks that are not held to the zero-false-"
            "alarm bar of the red list, so this bucket <i>may</i> include the "
            "occasional false alarm. It surfaces classes the strict checks stay "
            "silent on (e.g. a value or column order inside a wide movement "
            "matrix). Scan it when you have time; a red list item always takes "
            f"priority (click to expand)</summary>{tier_rows('caution')}"
            "</details>"
        )
    if layout_n:
        issue_table += (
            f"<details><summary><b>Layout artifacts ({layout_n})</b> — every "
            "word verified on the cited PDF page, but the print layout wraps "
            "the columns so exact order could not be machine-proved; a quick "
            f"glance suffices (click to expand)</summary>{tier_rows('layout')}"
            "</details>"
        )
    if not result.issues:
        issue_table = (
            "<p><b>No inconsistencies found — every figure and text block "
            "was validated against the PDF.</b></p>"
        )

    index_note = (
        f"<p>ℹ {result.coverage.index_entries} print-index entries: labels "
        "validated; the page-number column is print-only and was excluded "
        "from figure checks (an unpaginated HTML carries no page numbers).</p>"
        if result.coverage.index_entries
        else ""
    )

    # Assurance & scope — the tool states its OWN coverage so a reviewer can
    # see exactly what was and was not machine-verified.
    cov = result.coverage
    figs_pct = (
        round(100 * result.figures_ok / result.figures_total)
        if result.figures_total
        else 100
    )
    rows_pct = (
        round(100 * cov.rows_value_checked / cov.rows_with_figures)
        if cov.rows_with_figures
        else 100
    )
    skip_bits = "".join(
        f"<li>{n} — {html_mod.escape(reason)}: verified for presence, count "
        "and sign, but not for column order (a human should eyeball these)</li>"
        for reason, n in cov.rows_value_skipped.most_common()
    )
    warn_pages = (
        f'<p class="secv-assure-warn">⚠ {len(cov.low_text_pages)} PDF page(s) '
        f"({', '.join(cov.low_text_pages[:10])}) yielded almost no text — they "
        "may be scanned/image content that <b>cannot be read or checked</b>. "
        "Review those pages manually.</p>"
        if cov.low_text_pages
        else ""
    )
    scope_note = (
        "Strict review ON: every figure — including those below 100 and "
        "reference numbers — is machine-verified in place or blue-marked for "
        "review; no number is out of scope. Verify separately: totals/subtotals "
        "footing (run before this tool, then pass --footed); purely visual "
        "formatting (bold, indentation, colour) and CSS-driven visual "
        "reordering."
        if strict else
        "Not in scope (verify separately): totals/subtotals footing (use "
        "<code>fincheck</code>); figures below 100 and outline/reference "
        "numbers are presence-checked only — run with --strict to have every "
        "number reviewed; purely visual formatting (bold, indentation, colour) "
        "and CSS-driven visual reordering."
    )
    assurance = f"""
<div id="secv-assurance">
<h3 style="margin:0 0 4px 0">Assurance &amp; scope — what was machine-verified</h3>
<ul style="margin:2px 0">
<li><b>Text:</b> {cov.ok}/{cov.total} PDF lines reflected in the HTML; every
HTML sentence checked back against the PDF.</li>
<li><b>Figures — presence &amp; sign:</b> {result.figures_ok}/{result.figures_total}
({figs_pct}%) HTML figures found in the PDF; the sign (positive/negative) of
every significant figure reconciled document-wide.</li>
<li><b>Figures — placement &amp; order:</b> full value/column-order integrity
verified on {cov.rows_value_checked}/{cov.rows_with_figures} ({rows_pct}%) of
figure-bearing rows.{" The remainder:" if skip_bits else ""}</li>
</ul>
{f'<ul style="margin:0 0 2px 18px">{skip_bits}</ul>' if skip_bits else ''}
{warn_pages}
<p style="font-size:8pt;color:#555;margin:4px 0 0">{scope_note}</p>
</div>
"""

    missing_rows = "".join(
        f"<tr><td>{html_mod.escape(m['figure'])}</td>"
        f"<td>{', '.join(str(p) for p in m['pdf_pages'][:8])}</td>"
        f"<td>{m['occurrences']}</td></tr>"
        for m in result.pdf_figures_missing[:100]
    )
    missing_section = (
        f"<details><summary><b>⚠ {len(result.pdf_figures_missing)} significant PDF "
        "figures never appear in the HTML</b> (possible omissions — click to "
        "expand)</summary><table><tr><th>PDF figure</th><th>PDF page(s)</th>"
        f"<th>Count</th></tr>{missing_rows}</table></details>"
        if result.pdf_figures_missing
        else "<p>Every significant PDF figure also appears in the HTML.</p>"
    )

    review_lines = [
        line
        for line in result.coverage.lines
        if line.status == "review" and not line.escalate
    ]
    review_section = ""
    if review_lines:
        rows_r = "".join(
            f"<tr><td>{line.label or line.page}</td><td>{html_mod.escape(line.text[:160])}</td>"
            f"<td>{html_mod.escape(line.remark)}</td></tr>"
            for line in review_lines
        )
        review_section = (
            f"<details><summary><b>{len(review_lines)} PDF lines to review "
            "manually</b> (reflected in the HTML but not verbatim — mostly "
            "signature blocks and table headers whose reading order differs; "
            "click to expand)</summary><table><tr><th>PDF page</th>"
            f"<th>PDF line</th><th>Remark</th></tr>{rows_r}</table></details>"
        )

    unplaced_section = ""
    if unplaced:
        items = "".join(
            f'<li id="secv-cov-{idx}"><b>PDF {line.label or line.page}:</b> '
            f"{html_mod.escape(line.text)} — <i>{html_mod.escape(line.remark)}</i></li>"
            for idx, line in unplaced
        )
        unplaced_section = (
            "<p><b>⚠ Omissions that could not be positioned inline</b> "
            "(no nearby matching content to attach them to):</p>"
            f"<ul>{items}</ul>"
        )

    zone_legend = ""
    zone_note = ""
    if zone_counts is not None:
        figs_z, xref_z, intable_z, ctx_z = zone_counts
        zone_legend = (
            '<span class="secv-num-review">blue = manual-review zone '
            "(verify by eye)</span>"
        )
        strict_lead = (
            "<b>🔵 STRICT manual-review ON — no number left un-checked.</b> "
            "Every prose number (however small) and every in-table figure whose "
            "row did not match the PDF outright is painted "
            if strict else
            "<b>🔵 Manual-review overlay ON.</b> The tool cannot machine-verify "
            "errors that leave every token in place — a value moved to the wrong "
            "spot (Class 2) or a wrong note/schedule cross-reference (Class 3). To "
            "leave no chance on these, the spots where they could hide are painted "
        )
        zone_note = (
            f'<p style="background:#eaf3ff;border:1px solid #1560c0;padding:6px 10px">'
            f"{strict_lead}"
            f"<span class='secv-num-review'>blue</span> — <i>locations to check by "
            f"eye</i>, not errors: <b>{figs_z}</b> prose figures, <b>{intable_z}</b> "
            f"in-table figures whose row could not be confirmed as a whole, "
            f"<b>{ctx_z}</b> repeated-label figures the tool could not pin to a "
            f"single context (a mismatch, or a mirrored segment/hierarchy table "
            f"with no distinctive anchor), and <b>{xref_z}</b> cross-references. "
            f"Figures whose entire sentence or table row matched the PDF "
            f"<b>verbatim</b> (words and numbers, in order) are machine-validated "
            f"and stay green — blue remains only where that verbatim confirmation "
            f"failed.</p>"
        )

    n_act = sum(1 for i in result.issues if i.severity == "error")
    n_check = sum(1 for i in result.issues if i.severity in ("review", "caution"))
    figs_pct_hdr = (
        round(100.0 * result.figures_ok / result.figures_total, 1)
        if result.figures_total else 100.0
    )
    _chip = ("display:inline-block;padding:8px 18px;margin:4px 8px 4px 0;"
             "border-radius:6px;font-size:13pt;font-weight:bold")
    headline = (
        f'<div style="margin:6px 0 10px 0">'
        f'<span style="{_chip};background:#e2f7e1;border:2px solid #1e8a26;color:#1e6b24">'
        f'✓ {result.figures_ok:,} of {result.figures_total:,} figures verified '
        f'({figs_pct_hdr}%)</span>'
        + (f'<span style="{_chip};background:#ffecec;border:2px solid #a00000;color:#a00000">'
           f'✗ {n_act} item(s) to act on</span>' if n_act else
           f'<span style="{_chip};background:#e2f7e1;border:2px solid #1e8a26;color:#1e6b24">'
           f'no discrepancies</span>')
        + (f'<span style="{_chip};background:#fff7e0;border:2px solid #9a6a00;color:#9a6a00">'
           f'{n_check} quick check(s)</span>' if n_check else "")
        + '</div>'
        '<p style="margin:2px 0 8px 0;font-size:10pt">How to read this copy: '
        '<span class="secv-num-ok">green = verified</span> · '
        '<span class="secv-text-bad">red = fix / verify against source</span> · '
        '<span class="secv-text-warn">amber = check wording</span> · '
        + ('<span class="secv-num-review">blue = check by eye</span> · '
           if zone_counts is not None else '')
        + '<span class="secv-num-minor">bright yellow</span> = small/immaterial '
          'value (list marker, year, sub-100 count) — present in the PDF but '
          'not independently verified, so never stamped green · '
        + 'hover anything coloured for the exact reason.</p>'
    )

    banner_html = f"""
<div id="secv-summary">
<h2 style="margin:0 0 2px 0">secverify — review copy</h2>
<p style="margin:0 0 4px 0;font-size:9pt;color:#555"><b>PDF:</b> {html_mod.escape(pdf_name)}
&nbsp;·&nbsp; <b>HTML:</b> {html_mod.escape(html_name)}</p>
{headline}
<h3 style="margin:8px 0 0 0">Items to correct ({len(result.issues)})</h3>
{issue_table}
<details style="margin-top:10px"><summary style="font-weight:bold;cursor:pointer">
Scope, method &amp; detailed statistics (what was machine-checked — click to expand)</summary>
{zone_note}
<p><b>HTML → PDF &nbsp;·&nbsp; Figures:</b> {ok_pct} validated ({result.figures_bad} not found) &nbsp;·&nbsp;
<b>Text blocks:</b> {result.text_blocks_ok}/{result.text_blocks_total} matched,
{result.text_blocks_review} need review, {result.text_blocks_bad} not found</p>
<p><b>PDF → HTML coverage:</b> {result.coverage.ok}/{result.coverage.total} PDF lines
reflected in the HTML, {result.coverage.review} to review,
<span class="{'sev-error' if result.coverage.missing else ''}">{result.coverage.missing} missing</span>,
<span class="{'sev-error' if any(not o.review for o in result.coverage.order_issues) else ''}">{sum(1 for o in result.coverage.order_issues if not o.review)} content-order violation(s)</span>,
<span class="{'sev-review' if any(o.review for o in result.coverage.order_issues) else ''}">{sum(1 for o in result.coverage.order_issues if o.review)} relocated paragraph(s) to review</span>.
Omitted PDF content is shown <b>inline</b> as a red callout box at the exact
position in this document where it should have appeared.</p>
{assurance}
{index_note}
</details>
{missing_section}
{review_section}
{unplaced_section}
<p style="font-size:8pt;color:#555">Generated offline by secverify. Hover any
red/amber highlight for its remark; [n] markers link back to this panel.
This annotated copy is for review only — do not file it.</p>
</div>
"""
    banner = BeautifulSoup(banner_html, "html.parser")
    root.insert(0, banner)
