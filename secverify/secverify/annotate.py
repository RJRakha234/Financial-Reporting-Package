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
from .textnorm import canonical, find_best_match, split_sentences

BLOCK_TAGS = ("p", "td", "th", "li", "caption", "h1", "h2", "h3", "h4", "h5", "h6", "div")
SKIP_PARENTS = {"script", "style", "title", "head"}
FUZZY_REVIEW_RATIO = 0.80  # ≥ this but not exact → amber "review"


@dataclass
class Issue:
    num: int
    kind: str      # "figure" | "text"
    severity: str  # "error" | "review"
    excerpt: str
    remark: str
    anchor: str


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
    def __init__(self, corpus: PdfCorpus):
        self.corpus = corpus
        self.result = Result()
        self._issue_seq = 0

    # -- issues ----------------------------------------------------------
    def _new_issue(self, kind: str, severity: str, excerpt: str, remark: str) -> Issue:
        self._issue_seq += 1
        issue = Issue(
            num=self._issue_seq,
            kind=kind,
            severity=severity,
            excerpt=" ".join(excerpt.split())[:160],
            remark=remark,
            anchor=f"secv-i{self._issue_seq}",
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
                    span["title"] = "Validated: found in PDF on page(s) " + ", ".join(
                        str(p) for p in pages[:6]
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
                        "pdf_pages": self.corpus.pages_for_number(key),
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
                        "pdf_pages": self.corpus.pages_for_number(key),
                    }
                )

    # -- text ------------------------------------------------------------
    def _check_sentence(self, sentence: str) -> tuple[str, str]:
        """Return ``(status, remark)``; status ∈ ok / review / error / skip."""
        canon = canonical(sentence)
        if not canon or canon.isdigit():
            return "skip", ""
        # Tier 1: exact match including figures.
        if canon in self.corpus.alnum.canon:
            return "ok", ""
        # Tier 2: exact match of the words alone.  PDF extraction interleaves
        # table figures/headers into label text unpredictably; the figures
        # themselves are validated by the separate number pass.
        letters = canonical(sentence, letters_only=True)
        if letters and letters in self.corpus.letters.canon:
            return "ok", ""
        needle = letters or canon
        view = self.corpus.letters if letters else self.corpus.alnum
        match = find_best_match(view.canon, needle)
        if match is None:
            coverage = self._word_coverage(sentence)
            if coverage is not None:
                page, pct = coverage
                return "review", (
                    f"“{_shorten(sentence)}” is not contiguous in the PDF, but "
                    f"{pct:.0%} of its words appear together on PDF page {page} — "
                    "typically a multi-column table header that wraps onto "
                    "several lines in the PDF. Verify that page manually."
                )
            if len(needle) < 12:
                # Too short to fuzzy-match; only exact lookup was possible.
                return "error", (
                    f"“{sentence.strip()}” was not found in the PDF."
                )
            return "error", (
                f"Not found in the PDF: “{_shorten(sentence)}”. "
                "No similar passage exists — this content may be missing from "
                "or added relative to the PDF."
            )
        start, end, ratio = match
        page = view.page_of(start)
        snippet = self.corpus.raw_snippet(view, start, end)
        if ratio >= FUZZY_REVIEW_RATIO:
            return "review", (
                f"Close but not identical to the PDF (similarity {ratio:.0%}). "
                f"HTML says: “{_shorten(sentence)}”. "
                f"PDF page {page} says: “{snippet}”. Reconcile the wording/figures."
            )
        coverage = self._word_coverage(sentence)
        if coverage is not None:
            cov_page, pct = coverage
            return "review", (
                f"“{_shorten(sentence)}” is not contiguous in the PDF, but "
                f"{pct:.0%} of its words appear together on PDF page {cov_page} — "
                "typically a table header/label whose columns the PDF wraps "
                f"differently. Nearest contiguous passage (page {page}, "
                f"similarity {ratio:.0%}): “{snippet}”. Verify manually."
            )
        return "error", (
            f"Does not match the PDF. HTML says: “{_shorten(sentence)}”. "
            f"The nearest passage (PDF page {page}, similarity {ratio:.0%}) is: "
            f"“{snippet}”."
        )

    def _word_coverage(self, sentence: str) -> tuple[int, float] | None:
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
        best: tuple[int, float] | None = None
        for page_idx, page_canon in enumerate(self.corpus.page_letters):
            if not page_canon:
                continue
            hit = sum(1 for w in words if w in page_canon)
            pct = hit / len(words)
            if best is None or pct > best[1]:
                best = (page_idx + 1, pct)
        if best and best[1] >= 0.85:
            return best
        return None

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
            for sentence in split_sentences(text):
                status, remark = self._check_sentence(sentence)
                if status == "skip":
                    continue
                if status == "review" and worst == "ok":
                    worst = "review"
                if status == "error":
                    worst = "error"
                if remark:
                    remarks.append(remark)

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
            issue = self._new_issue("text", severity, text, remark)
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
        self.html_corpus = HtmlCorpus(root.get_text(" "))
        self.result.coverage = check_pdf_coverage(
            self.corpus.pages_raw,
            self.html_corpus,
            self.corpus.alnum.canon,
            self.corpus.letters.canon,
        )
        self._block_index = _collect_block_index(root)
        self._annotate_blocks(soup, root)
        self._annotate_numbers(soup, root)
        cov_anchors = self._register_coverage_issues()
        unplaced = self._insert_inline_omissions(soup, cov_anchors)
        _inject_banner(soup, root, self.result, pdf_name, html_name, unplaced)
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
            heading = (
                "MISSING FROM HTML"
                if line.status == "missing"
                else "CHECK — POSSIBLY DROPPED"
            )
            text = (
                f"⛔ {heading} · PDF page {line.page}: “{line.text}” — "
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
                Comment(f" SECVERIFY OMISSION (PDF p.{line.page}): {line.remark} ")
            )
        return unplaced

    def _match_block(self, a_letters: str, frac: float):
        """Find the DOM block reflecting *a_letters*, or None."""
        if len(a_letters) < 10:
            return None
        matches = [
            b
            for b in self._block_index
            if (b["letters"] and a_letters in b["letters"])
            or (len(b["letters"]) >= 10 and b["letters"] in a_letters)
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
        # A count-shortfall line IS present in the HTML (just fewer times):
        # anchor the callout on the reflected instance itself.
        if line.status != "missing":
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
                "omission",
                severity,
                f"(PDF p.{line.page}) {line.text}",
                line.remark,
            )
            # The highlight for an omission is an inline callout box (or a
            # summary-panel entry when it cannot be positioned).
            issue.anchor = f"secv-cov-{idx}"
            anchors[idx] = issue.anchor
        for m in self.result.figure_count_mismatches:
            pages = ", ".join(f"p.{p}" for p in m["pdf_pages"][:5])
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
                "letters": canonical(text, letters_only=True),
                "pos": len(index),
            }
        )
    return index


_CSS = """
.secv-num-ok { background: #52d05c; border-radius: 2px; padding: 0 1px; }
.secv-num-bad { background: #ff6b6b; outline: 2px solid #a00000; border-radius: 2px;
                font-weight: bold; padding: 0 1px; }
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
.secv-legend span { padding: 1px 6px; margin-right: 10px; }
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
) -> None:
    style = soup.new_tag("style")
    style.string = _CSS
    head = soup.head
    (head or root).insert(0, style)

    ok_pct = (
        f"{result.figures_ok}/{result.figures_total}"
        if result.figures_total
        else "0/0"
    )
    rows = []
    for issue in result.issues:
        rows.append(
            f'<tr><td><a href="#{issue.anchor}">#{issue.num}</a></td>'
            f'<td>{issue.kind}</td>'
            f'<td class="sev-{issue.severity}">{issue.severity.upper()}</td>'
            f"<td>{html_mod.escape(issue.excerpt)}</td>"
            f"<td>{html_mod.escape(issue.remark)}</td></tr>"
        )
    issue_table = (
        "<table><tr><th>#</th><th>Type</th><th>Severity</th>"
        "<th>HTML content</th><th>Remark — what to correct</th></tr>"
        + "".join(rows)
        + "</table>"
        if rows
        else "<p><b>No inconsistencies found — every figure and text block was validated against the PDF.</b></p>"
    )

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
            f"<tr><td>p.{line.page}</td><td>{html_mod.escape(line.text[:160])}</td>"
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
            f'<li id="secv-cov-{idx}"><b>PDF p.{line.page}:</b> '
            f"{html_mod.escape(line.text)} — <i>{html_mod.escape(line.remark)}</i></li>"
            for idx, line in unplaced
        )
        unplaced_section = (
            "<p><b>⚠ Omissions that could not be positioned inline</b> "
            "(no nearby matching content to attach them to):</p>"
            f"<ul>{items}</ul>"
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
</p>
<p><b>HTML → PDF &nbsp;·&nbsp; Figures:</b> {ok_pct} validated ({result.figures_bad} not found) &nbsp;·&nbsp;
<b>Text blocks:</b> {result.text_blocks_ok}/{result.text_blocks_total} matched,
{result.text_blocks_review} need review, {result.text_blocks_bad} not found</p>
<p><b>PDF → HTML coverage:</b> {result.coverage.ok}/{result.coverage.total} PDF lines
reflected in the HTML, {result.coverage.review} to review,
<span class="{'sev-error' if result.coverage.missing else ''}">{result.coverage.missing} missing</span>.
Omitted PDF content is shown <b>inline</b> as a red callout box at the exact
position in this document where it should have appeared.</p>
<h3 style="margin:8px 0 0 0">Items to correct ({len(result.issues)})</h3>
{issue_table}
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
