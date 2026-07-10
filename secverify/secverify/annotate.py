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
    ):
        self.corpus = corpus
        self.result = Result()
        self._issue_seq = 0
        self.level = level
        self.pdf_paths = pdf_paths or []
        self.review_zones = review_zones
        self._zone_counts = (0, 0, 0)

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
                self.result.figures_total += 1
                ok = self.corpus.has_number(key)
                if start > cursor:
                    fragments.append(text[cursor:start])
                span = soup.new_tag("span")
                span.string = token
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

        for kind, sev, exc, rem in render_and_hidden_checks(
            self.corpus, clean_soup, self.pdf_paths
        ):
            self._new_issue(kind, sev, exc, rem)
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

    # -- text ------------------------------------------------------------
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
                # Too short to fuzzy-match; only exact lookup was possible.
                return "error", (
                    f"“{sentence.strip()}” was not found in the PDF."
                ), "absent"
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
        self._identifier_census()
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
        if self.review_zones:
            from .reviewzones import mark_review_zones

            self._zone_counts = mark_review_zones(soup, root, self.corpus)
        _inject_banner(
            soup, root, self.result, pdf_name, html_name, unplaced,
            zone_counts=self._zone_counts if self.review_zones else None,
        )
        self.result.html_out = str(soup)
        return self.result

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
        for oi in self.result.coverage.order_issues:
            span = (
                f"“{_shorten(oi.first_text, 100)}”"
                if oi.count == 1
                else f"“{_shorten(oi.first_text, 80)}” … “{_shorten(oi.last_text, 80)}”"
                f" ({oi.count} lines)"
            )
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
.secv-num-bad { background: #ff6b6b; outline: 2px solid #a00000; border-radius: 2px;
                font-weight: bold; padding: 0 1px; }
.secv-num-review { background: #7cb8ff; outline: 1px solid #1560c0; border-radius: 2px;
                   padding: 0 1px; }
.secv-xref-review { background: #7cb8ff; outline: 1px solid #1560c0; border-radius: 2px;
                    padding: 0 1px; }
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
<p style="font-size:8pt;color:#555;margin:4px 0 0">Not in scope (verify
separately): totals/subtotals footing (use <code>fincheck</code>); figures
below 100 and outline/reference numbers; purely visual formatting
(bold, indentation, colour) and any CSS-driven reordering or hidden text in
the HTML.</p>
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
        figs_z, xref_z, intable_z = zone_counts
        zone_legend = (
            '<span class="secv-num-review">blue = manual-review zone '
            "(verify by eye)</span>"
        )
        zone_note = (
            f'<p style="background:#eaf3ff;border:1px solid #1560c0;padding:6px 10px">'
            f"<b>🔵 Manual-review overlay ON.</b> The tool cannot machine-verify "
            f"errors that leave every token in place — a value moved to the wrong "
            f"spot (Class 2) or a wrong note/schedule cross-reference (Class 3). To "
            f"leave no chance on these, the spots where they could hide are painted "
            f"<span class='secv-num-review'>blue</span> — <i>locations to check by "
            f"eye</i>, not errors: <b>{figs_z}</b> prose figures, <b>{intable_z}</b> "
            f"in-table figures whose row could not be confirmed as a whole against "
            f"the PDF, and <b>{xref_z}</b> cross-references. Rows the tool confirmed "
            f"exactly stay green.</p>"
        )

    banner_html = f"""
<div id="secv-summary">
<h2>secverify — PDF ↔ HTML validation report</h2>
<p><b>Reference PDF:</b> {html_mod.escape(pdf_name)} &nbsp;·&nbsp;
<b>Checked HTML:</b> {html_mod.escape(html_name)}</p>
<p class="secv-legend">
<span class="secv-num-ok">green figure = validated against PDF</span>
<span class="secv-num-bad">red figure = not in PDF</span>
<span class="secv-text-ok">green block = text matches PDF</span>
<span class="secv-text-warn">amber block = close match, review wording</span>
<span class="secv-text-bad">red block = text not in PDF</span>
{zone_legend}
</p>
{zone_note}
<p><b>HTML → PDF &nbsp;·&nbsp; Figures:</b> {ok_pct} validated ({result.figures_bad} not found) &nbsp;·&nbsp;
<b>Text blocks:</b> {result.text_blocks_ok}/{result.text_blocks_total} matched,
{result.text_blocks_review} need review, {result.text_blocks_bad} not found</p>
<p><b>PDF → HTML coverage:</b> {result.coverage.ok}/{result.coverage.total} PDF lines
reflected in the HTML, {result.coverage.review} to review,
<span class="{'sev-error' if result.coverage.missing else ''}">{result.coverage.missing} missing</span>,
<span class="{'sev-error' if result.coverage.order_issues else ''}">{len(result.coverage.order_issues)} content-order violation(s)</span>.
Omitted PDF content is shown <b>inline</b> as a red callout box at the exact
position in this document where it should have appeared; the content-order
check verifies the HTML presents the PDF's content in the PDF's sequence.</p>
{assurance}
<h3 style="margin:8px 0 0 0">Items to correct ({len(result.issues)})</h3>
{issue_table}
{index_note}
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
