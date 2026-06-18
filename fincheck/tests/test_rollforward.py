"""Unit-level comparison logic plus an end-to-end run on the sample filings."""

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from fincheck import check_rollforward
from fincheck.extract import Cell, Row
from fincheck.periods import Period
from fincheck.rollforward import Figure, _review_sections, _section_of, compare


def _hrow(tokens):
    return Row(
        0, 0, 10, "", 0.0,
        tokens=[
            {"text": t, "x0": i * 30, "x1": i * 30 + 20, "top": 0, "bottom": 8}
            for i, t in enumerate(tokens)
        ],
    )


def test_section_of_reads_note_number_from_tokens():
    assert _section_of(_hrow(["2.4", "Prepayments", "and", "other"])) == "2.4"
    assert _section_of(_hrow(["2.3.4", "Fair", "value"])) == "2.3.4"
    # An extraction artifact glued to the number is stripped.
    assert _section_of(_hrow(["X14AO2.11", "Employees", "Stock"])) == "2.11"
    # A note *reference* mid-row (not a heading) is not a section.
    assert _section_of(_hrow(["Cash", "equivalents", "2.1", "24,455"])) is None


def test_section_of_statement_title_requires_consolidated():
    assert _section_of(_hrow(["Condensed", "Consolidated", "Balance", "Sheet"])) == "BS"
    # Prose mentioning a statement is not a heading.
    assert _section_of(_hrow(["recognised", "in", "comprehensive", "income"])) is None


def test_review_flags_structural_mismatch_keeps_consistent_repeats():
    d = date(2025, 3, 31)
    # ESOP-like: the label occurs a different number of times across filings.
    esop_cur = [
        _fig("c", d, "year", "KMP", 10, occ=1, section="2.11"),
        _fig("c", d, "year", "KMP", 20, occ=2, section="2.11"),
    ]
    esop_pri = [_fig("p", d, "year", "KMP", 10, occ=1, section="2.11")]
    assert "2.11" in _review_sections(esop_cur, [esop_pri])

    # Current/non-current split: same label twice on *both* sides — keep it.
    note_cur = [
        _fig("c", d, "year", "Deposits", 1, occ=1, section="2.4"),
        _fig("c", d, "year", "Deposits", 2, occ=2, section="2.4"),
    ]
    note_pri = [
        _fig("p", d, "year", "Deposits", 1, occ=1, section="2.4"),
        _fig("p", d, "year", "Deposits", 2, occ=2, section="2.4"),
    ]
    assert "2.4" not in _review_sections(note_cur, [note_pri])

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "rf_current.pdf"
PRIOR = ROOT / "rf_prior.pdf"


@pytest.fixture(scope="session", autouse=True)
def ensure_samples():
    if not (CURRENT.exists() and PRIOR.exists()):
        subprocess.run(
            [sys.executable, str(ROOT / "make_rollforward_sample.py")], check=True
        )


def _fig(source, d, ptype, label, value, occ=1, section=""):
    period = Period(0, d, ptype)
    return Figure(
        source=source, section=section, end_date=d, period_type=ptype,
        period=period, label=label, norm_label=label.lower(), occurrence=occ,
        value=value, page_index=0, cell=Cell(0, value, str(value), 0, 0, 0, 0),
    )


def test_compare_flags_only_changed_figures():
    d = date(2025, 6, 30)
    current = [
        _fig("cur", d, "quarter", "Revenue", 100),
        _fig("cur", d, "quarter", "Other income", 120),
    ]
    prior = [
        _fig("pri", d, "quarter", "Revenue", 100),
        _fig("pri", d, "quarter", "Other income", 95),
    ]
    checks, covered, _ = compare(current, [prior], base_tolerance=1.0)
    status = {c.current.label: c.status for c in checks}
    assert status == {"Revenue": "ok", "Other income": "mismatch"}
    assert (d, "quarter") in covered


def test_compare_requires_matching_period_type():
    d = date(2026, 3, 31)
    # Quarter-ended 31 Mar and year-ended 31 Mar are different numbers.
    current = [_fig("cur", d, "quarter", "Revenue", 50)]
    prior = [_fig("pri", d, "year", "Revenue", 200)]
    checks, _, _ = compare(current, [prior], base_tolerance=1.0)
    assert checks == []


def test_compare_unknown_type_is_wildcard():
    d = date(2025, 6, 30)
    current = [_fig("cur", d, "unknown", "Cash", 10)]
    prior = [_fig("pri", d, "quarter", "Cash", 99)]
    checks, _, _ = compare(current, [prior], base_tolerance=1.0)
    assert len(checks) == 1 and checks[0].status == "mismatch"


def test_same_date_different_type_not_treated_as_duplicate():
    # An income statement shows the same date for a quarter and a half-year
    # column; both must be checked, not dropped as ambiguous duplicates.
    d = date(2024, 9, 30)
    current = [
        _fig("cur", d, "quarter", "Revenue", 44490),
        _fig("cur", d, "half-year", "Revenue", 86769),
    ]
    prior = [
        _fig("pri", d, "quarter", "Revenue", 44490),
        _fig("pri", d, "half-year", "Revenue", 80000),  # differs
    ]
    checks, _, ambiguous = compare(current, [prior], base_tolerance=1.0)
    status = {(c.current.period_type): c.status for c in checks}
    assert status == {"quarter": "ok", "half-year": "mismatch"}
    assert ambiguous == 0


def test_section_disambiguates_same_label():
    d = date(2025, 3, 31)
    # "Total" appears in two different notes; each must match its own note.
    current = [
        _fig("cur", d, "year", "Total", 100, section="2.4"),
        _fig("cur", d, "year", "Total", 200, section="2.5"),
    ]
    prior = [
        _fig("pri", d, "year", "Total", 100, section="2.4"),
        _fig("pri", d, "year", "Total", 999, section="2.5"),  # restated
    ]
    checks, _, _ = compare(current, [prior], base_tolerance=1.0)
    status = {c.current.section: c.status for c in checks}
    assert status == {"2.4": "ok", "2.5": "mismatch"}


def test_repeated_label_in_one_section_pairs_by_occurrence():
    d = date(2025, 3, 31)
    current = [
        _fig("cur", d, "year", "Total", 100, occ=1, section="2.6"),
        _fig("cur", d, "year", "Total", 200, occ=2, section="2.6"),
    ]
    prior = [
        _fig("pri", d, "year", "Total", 100, occ=1, section="2.6"),
        _fig("pri", d, "year", "Total", 222, occ=2, section="2.6"),
    ]
    checks, _, _ = compare(current, [prior], base_tolerance=1.0)
    status = {c.current.occurrence: c.status for c in checks}
    assert status == {1: "ok", 2: "mismatch"}


def test_prior_disagreement_is_ambiguous():
    d = date(2025, 3, 31)
    current = [_fig("cur", d, "year", "Total", 100, section="2.6")]
    priors = [
        [_fig("a", d, "year", "Total", 100, section="2.6")],
        [_fig("b", d, "year", "Total", 105, section="2.6")],  # priors disagree
    ]
    checks, _, ambiguous = compare(current, priors, base_tolerance=1.0)
    assert checks == [] and ambiguous == 1


def test_no_prior_figure_means_not_checked():
    current = [_fig("cur", date(2026, 6, 30), "quarter", "Revenue", 100)]
    prior = [_fig("pri", date(2025, 6, 30), "quarter", "Revenue", 100)]
    checks, _, _ = compare(current, [prior], base_tolerance=1.0)
    assert checks == []  # periods don't overlap


def test_end_to_end_sample_catches_rollforward_error():
    result = check_rollforward(str(CURRENT), [str(PRIOR)])
    assert result.figures_checked == 12
    assert not result.consistent

    mismatched = {c.current.label.strip().lower() for c in result.mismatches}
    # The mis-keyed Other income and everything it flows into are flagged.
    assert "other income" in mismatched
    assert "total income" in mismatched
    # Figures that were rolled forward correctly are not flagged.
    assert "revenue from operations" not in mismatched
    assert "tax expense" not in mismatched


def test_end_to_end_reports_uncovered_period():
    result = check_rollforward(str(CURRENT), [str(PRIOR)])
    described = {p.describe() for p in result.uncovered_periods}
    # The year-ended-Mar-2026 comparative has no matching prior filing supplied.
    assert any("31 Mar 2026" in d for d in described)
    # The current reporting period itself is never reported as uncovered.
    assert not any("30 Jun 2026" in d for d in described)


def test_end_to_end_writes_highlighted_pdf(tmp_path):
    out = tmp_path / "rf.pdf"
    result = check_rollforward(str(CURRENT), [str(PRIOR)], output_pdf=str(out))
    assert out.exists() and result.output_pdf == str(out)
    import fitz

    doc = fitz.open(str(out))
    assert doc.page_count == 2  # statement page + prepended summary
    annot_types = [a.type[1] for a in doc[1].annots()]
    assert annot_types.count("Square") == 4  # four mismatches outlined
    assert annot_types.count("Highlight") == 12  # every checked figure marked
