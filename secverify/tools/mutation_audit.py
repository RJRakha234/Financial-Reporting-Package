"""Adversarial audit: inject known conversion errors, check the tool flags them.

A checker is only worth its green marks if a wrong document reliably fails it.
This harness takes a REAL exhibit and its source PDF, applies one deliberate
conversion error at a time to the HTML, re-runs the full tool, and reports
whether that error surfaced as something other than green.

Two independent signals decide the verdict, because either alone can mislead:

*new findings*
    Issues present after the mutation that were not in the baseline run.  The
    baseline is subtracted so a filing's own pre-existing findings (an exhibit
    label, an image-borne figure) cannot be mistaken for detection.

*the mark on the mutated token*
    The CSS class the review copy actually paints on the changed value —
    ``secv-num-ok`` (green) versus bad / review / minor.  This is the signal a
    human reviewer sees, so a mutation that produces an issue *somewhere* but
    still paints the changed number green is reported as such rather than as a
    clean catch.

Usage::

    python tools/mutation_audit.py statement.pdf [more.pdf ...] exhibit.html

Every mutation must actually apply: a pattern that does not occur in the HTML is
reported as SKIPPED rather than silently passing, so the catalogue cannot drift
out of step with the document and quietly stop testing anything.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from secverify.annotate import Annotator  # noqa: E402
from secverify.pdfside import load_pdf, order_pdfs_to_html  # noqa: E402

GREEN = "secv-num-ok"


@dataclass
class Mutation:
    """One deliberate conversion error."""

    category: str
    name: str
    old: str
    new: str
    #: token to look for in the output to read its highlight class; defaults to
    #: the replacement text when it is a bare value
    probe: str | None = None
    notes: str = ""


@dataclass
class Outcome:
    mutation: Mutation
    applied: bool
    new_issues: list[tuple[str, str]] = field(default_factory=list)
    marks: set[str] = field(default_factory=set)
    figures_delta: int = 0

    @property
    def verdict(self) -> str:
        if not self.applied:
            return "SKIPPED"
        non_green = {m for m in self.marks if m != GREEN}
        if self.new_issues or non_green or self.figures_delta:
            return "CAUGHT"
        return "MISSED"


def _issue_sig(result) -> list[tuple[str, str]]:
    return [(i.kind, (i.excerpt or "")[:80]) for i in result.issues]


def _marks_for(html_out: str, probe: str) -> set[str]:
    """Highlight classes applied to *probe* in the annotated output."""
    marks: set[str] = set()
    for m in re.finditer(
        r'<span class="(secv-num-[a-z]+)"[^>]*>([^<]*)</span>', html_out
    ):
        if probe and probe in m.group(2):
            marks.add(m.group(1))
    return marks


def _statement_rows(soup):
    """The figure-bearing data rows of the largest statement table in *soup*."""
    best: list = []
    for t in soup.find_all("table"):
        idx = []
        for tr in t.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            figs = [t2 for t2 in texts if re.fullmatch(r"\(?[\d,]+\.?\d*\)?", t2 or "x")]
            label = next((t2 for t2 in texts if re.search(r"[A-Za-z]{4,}", t2)), "")
            if label and len(figs) >= 2:
                idx.append(tr)
        if len(idx) > len(best):
            best = idx
    return best


def _discover(html: str) -> list[Mutation]:
    """Mutations built from what THIS exhibit actually contains.

    A hardcoded catalogue silently stops testing whole error classes on a
    document that happens not to contain the literal pattern — the harness then
    reports a comfortable score while never exercising row swaps or sign flips at
    all.  These targets are read out of the document, so every class gets tested
    on every exhibit.
    """
    from bs4 import BeautifulSoup

    M = Mutation
    out: list[Mutation] = []
    soup = BeautifulSoup(html, "html.parser")

    # --- a parenthesised negative, for the sign check --------------------
    # Discovered from VISIBLE TEXT, never the raw markup: a stylesheet's
    # "rgb(204,238,255)" matches the same shape, and mutating a CSS colour tests
    # nothing while reporting a comfortable miss.
    visible = soup.get_text(" ")
    for m in re.finditer(r"\((\d{1,3}(?:,\d{3})+)\)", visible):
        val = m.group(1)
        if m.group(0) in html:
            out.append(
                M("sign", "negative shown as positive",
                  m.group(0), val, val.replace(",", ""))
            )
            break

    # --- two sibling table rows carrying the same number of figures ------
    rows: list[tuple[str, list[str], str]] = []
    for tr in _statement_rows(soup):
        texts = [
            c.get_text(" ", strip=True)
            for c in tr.find_all(["td", "th"], recursive=False)
        ]
        figs = [t for t in texts if re.fullmatch(r"\(?[\d,]+\.?\d*\)?", t or "x")]
        label = next((t for t in texts if re.search(r"[A-Za-z]{4,}", t)), "")
        rows.append((label, figs, str(tr)))

    if rows:
        label, figs, raw_tr = rows[0]
        # comparative columns transposed within one row
        swapped = raw_tr
        a, b = figs[0], figs[1]
        swapped = swapped.replace(f">{a}<", ">@@A@@<", 1).replace(f">{b}<", f">{a}<", 1)
        swapped = swapped.replace(">@@A@@<", f">{b}<", 1)
        if swapped != raw_tr:
            out.append(
                M("swap", "comparative columns transposed in a row",
                  raw_tr, swapped, b.replace(",", ""))
            )
    # A whole LINE relocated inside one table — label and values travelling
    # together.  Presence, row-value integrity and footing all still pass (same
    # rows, same figures, same totals), so row ORDER is the only signal there is.
    # Built by DOM surgery rather than string splicing: sibling <tr>s are
    # separated by spacer rows and whitespace, so "str(a) + str(b)" is not a
    # substring of the document and a string swap silently does nothing.
    if len(rows) >= 8:
        for src, dst, what in ((6, 3, "line 6 relocated to line 3"),
                               (5, 4, "adjacent lines swapped"),
                               (len(rows) - 1, 1, "last line relocated to the top")):
            s2 = BeautifulSoup(html, "html.parser")
            r2 = _statement_rows(s2)
            if max(src, dst) >= len(r2):
                continue
            r2[dst].insert_before(r2[src].extract())
            out.append(
                M("swap", f"whole line moved: {what}", html, str(s2), "",
                  "label and values travel together — only order changes")
            )

    if len(rows) >= 2:
        (l1, f1, tr1), (l2, f2, tr2) = rows[0], rows[1]
        # two rows exchange places (values stay correct, order wrong)
        both = tr1 + tr2
        if both in html:
            out.append(M("swap", "two table rows exchanged", both, tr2 + tr1, ""))
        # one row's figures copied onto the other row
        if len(f1) == len(f2) and f1 != f2:
            donor = tr2
            for x, y in zip(f2, f1):
                donor = donor.replace(f">{x}<", f">{y}<", 1)
            if donor != tr2:
                out.append(
                    M("swap", "one row's values copied onto another",
                      tr2, donor, f1[0].replace(",", ""))
                )
        # a whole row deleted
        out.append(M("structure", "table row deleted", tr2, "", ""))
        # a whole row duplicated
        out.append(M("structure", "table row duplicated", tr2, tr2 + tr2, ""))

    # --- a paragraph, for relocation / deletion / duplication ------------
    paras = [
        p for p in soup.find_all("p")
        if len(p.get_text(" ", strip=True)) > 80 and re.search(r"\d", p.get_text())
    ]
    if len(paras) >= 2:
        p1, p2 = str(paras[0]), str(paras[1])
        if p1 + p2 in html:
            out.append(M("structure", "two paragraphs reordered", p1 + p2, p2 + p1, ""))
        out.append(M("structure", "paragraph deleted", p1, "", ""))
        out.append(M("structure", "paragraph duplicated", p1, p1 + p1, ""))
    if paras:
        txt = paras[0].get_text(" ", strip=True)
        w = re.search(r"\b(is|are|was|were|has|have)\b", txt)
        if w:
            out.append(
                M("text", "negation inserted",
                  txt[: w.end()], txt[: w.end()] + " not", "")
            )

    # --- a hidden-text smuggle: real content made invisible --------------
    # The style must REPLACE any existing one, not sit beside it.  Duplicate
    # style attributes are resolved differently by different parsers (a browser
    # keeps the first, BeautifulSoup the last), so appending one produced markup
    # whose display:none was dropped before any check could see it — the harness
    # then reported a miss that was its own doing.
    if paras:
        p1 = str(paras[0])
        hidden = re.sub(r'\sstyle="[^"]*"', "", p1, count=1)
        hidden = hidden.replace("<p", '<p style="display:none"', 1)
        out.append(M("format", "content hidden via CSS", p1, hidden, ""))
    return out


def build_catalogue(html: str) -> list[Mutation]:
    """Conversion errors covering every class a reviewer worries about.

    Patterns are matched against the raw HTML, so a mutation that no longer
    occurs in a given exhibit is reported SKIPPED instead of vanishing.  The
    literal entries below are joined by :func:`_discover`, which reads further
    targets out of the document so every class is exercised on every exhibit.
    """
    M = Mutation
    cat: list[Mutation] = [
        # ---- figures -------------------------------------------------------
        M("figure", "digit changed in a large figure", "17,447", "17,347", "17347"),
        M("figure", "digits transposed", "17,419", "17,491", "17491"),
        M("figure", "extra digit inserted", "6,203", "62,203", "62203"),
        M("figure", "digit dropped", "6,060", "606", "606"),
        M("figure", "decimal point moved", "20.8%", "2.08%", "2.08"),
        M("figure", "percent sign dropped", "2.6% QoQ", "2.6 QoQ", "2.6"),
        M("figure", "small count changed", "55% Net New", "58% Net New", "58"),
        M("figure", "currency symbol swapped", "$3.8 Billion", "₹3.8 Billion", "3.8"),
        M("figure", "scale word swapped", "$3.8 Billion", "$3.8 Million", "3.8"),
        M("figure", "thousands separator dropped", "4,941", "4941", "4941"),
        # ---- signs ---------------------------------------------------------
        M("sign", "negative shown as positive", "(422", "422", "422"),
        # ---- values moved between rows -------------------------------------
        M("swap", "two line items' values exchanged",
          "Income tax expense 329 318", "Income tax expense 318 329", "318"),
        M("swap", "comparative columns transposed",
          "Total assets 17,447 17,419", "Total assets 17,419 17,447", "17419"),
        M("swap", "one row's value copied onto another",
          "Income tax expense 329", "Income tax expense 809", "809"),
        # ---- dates / periods ----------------------------------------------
        M("date", "year shifted", "June 30, 2025", "June 30, 2024", "2024"),
        M("date", "month changed", "June 30, 2025", "July 30, 2025", "30"),
        M("date", "release date changed", "July 23, 2025", "July 25, 2025", "25"),
        # ---- wording -------------------------------------------------------
        M("text", "negation inserted", "in CC, Driven by", "in CC, Not Driven by", ""),
        M("text", "meaning inverted", "Sequential Growth", "Sequential Decline", ""),
        M("text", "label changed", "Trade payables", "Trade receivables", ""),
        M("text", "entity name changed", "Infosys Limited", "Infosys Systems", ""),
        M("text", "guidance range altered", "20%-22%", "20%-24%", "24"),
        # ---- structure -----------------------------------------------------
        M("structure", "sentence deleted",
          "Bengaluru, India", "", "", "removes the dateline"),
        M("structure", "note reference changed", "Net New", "Net Old", ""),
    ]
    return cat + _discover(html)


def run_audit(pdf_paths: list[str], html_path: str) -> list[Outcome]:
    from bs4 import BeautifulSoup

    original = open(html_path, encoding="utf-8", errors="replace").read()
    # Normalise through the parser ONCE and audit that form.  Discovery reads
    # targets via BeautifulSoup, whose serialisation lower-cases tags and
    # re-orders attributes, so a `str(tr)` pattern never matched the exhibit's
    # raw uppercase <TR> markup — every structural mutation reported SKIPPED and
    # whole error classes went untested while the score looked fine.  Parsing
    # the same DOM twice is what the tool itself does, so this does not change
    # what is being checked.
    raw = str(BeautifulSoup(original, "html.parser"))
    ordered, _changed = order_pdfs_to_html(pdf_paths, raw)
    corpus = load_pdf(ordered)

    def run(html_text: str):
        # a fresh corpus per run: the annotator consumes corpus state, so
        # reusing one would let an earlier mutation colour a later verdict
        c = load_pdf(ordered)
        return Annotator(
            c, level="sigma", pdf_paths=list(ordered), strict=True
        ).run(html_text, "ref.pdf", "doc.html")

    print(f"baseline: {len(corpus.pages_raw)} PDF pages, {len(raw)} HTML chars")
    base = run(raw)
    base_sig = _issue_sig(base)
    base_counter: dict[tuple[str, str], int] = {}
    for s in base_sig:
        base_counter[s] = base_counter.get(s, 0) + 1
    print(
        f"baseline issues: {len(base.issues)} "
        f"| figures {base.figures_ok}/{base.figures_total}\n"
    )

    outcomes: list[Outcome] = []
    for mut in build_catalogue(raw):
        if mut.old not in raw:
            outcomes.append(Outcome(mut, applied=False))
            print(f"  SKIPPED  {mut.category:9} {mut.name}  (pattern not in exhibit)")
            continue
        mutated = raw.replace(mut.old, mut.new, 1)
        res = run(mutated)
        seen = dict(base_counter)
        new: list[tuple[str, str]] = []
        for s in _issue_sig(res):
            if seen.get(s):
                seen[s] -= 1
            else:
                new.append(s)
        probe = mut.probe if mut.probe is not None else ""
        out = Outcome(
            mutation=mut,
            applied=True,
            new_issues=new,
            marks=_marks_for(res.html_out, probe) if probe else set(),
            figures_delta=(base.figures_ok - base.figures_total)
            - (res.figures_ok - res.figures_total),
        )
        outcomes.append(out)
        kinds = sorted({k for k, _ in new})
        print(
            f"  {out.verdict:8} {mut.category:9} {mut.name}\n"
            f"           fired: {', '.join(kinds) if kinds else '-'}"
            f" | mark on changed value: {', '.join(sorted(out.marks)) or 'n/a'}"
        )
    return outcomes


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    *pdfs, html = argv
    t0 = time.time()
    outcomes = run_audit(pdfs, html)
    caught = [o for o in outcomes if o.verdict == "CAUGHT"]
    missed = [o for o in outcomes if o.verdict == "MISSED"]
    skipped = [o for o in outcomes if o.verdict == "SKIPPED"]
    print(f"\n{'=' * 72}")
    print(
        f"CAUGHT {len(caught)}/{len(caught) + len(missed)} applied mutations "
        f"({len(skipped)} skipped)  in {time.time() - t0:.0f}s"
    )
    if missed:
        print("\nMISSED — passed as green, a reviewer would see nothing:")
        for o in missed:
            print(f"   [{o.mutation.category}] {o.mutation.name}")
            print(f"        {o.mutation.old!r} -> {o.mutation.new!r}")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
