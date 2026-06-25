import json

from fincheck.notes.compare import diff_notes
from fincheck.notes.ledger import (
    ACCEPTED,
    Ledger,
    load_ledger,
    save_ledger,
    summarize,
)


def _one_difference():
    _, diffs = diff_notes(
        "t",
        "Note",
        "A",
        "The cost is amortized over five years per policy.",
        "B",
        "The cost is amortized over ten years per policy.",
    )
    assert diffs
    return diffs[0]


def test_accepted_difference_is_suppressed():
    diff = _one_difference()
    ledger = Ledger()
    assert summarize([diff], ledger) == {"open": 1, "accepted": 0, "ignored": 0}
    ledger.set(diff.hash, ACCEPTED, note=diff.title)
    assert ledger.is_resolved(diff.hash)
    assert summarize([diff], ledger) == {"open": 0, "accepted": 1, "ignored": 0}


def test_changed_wording_reopens_decision():
    diff = _one_difference()
    ledger = Ledger()
    ledger.set(diff.hash, ACCEPTED)
    # A diff whose wording changed has a different hash -> back to open.
    _, changed = diff_notes(
        "t",
        "Note",
        "A",
        "The cost is amortized over five years per policy.",
        "B",
        "The cost is written off over ten years per policy.",
    )
    assert ledger.status_of(changed[0].hash) == "open"


def test_ledger_round_trips_through_disk(tmp_path):
    diff = _one_difference()
    ledger = Ledger()
    ledger.set(diff.hash, ACCEPTED, note="Note", comment="expected wording change")
    path = tmp_path / "decisions.json"
    save_ledger(ledger, str(path))

    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 1
    assert on_disk["decisions"][diff.hash]["status"] == ACCEPTED

    reloaded = load_ledger(str(path))
    assert reloaded.status_of(diff.hash) == ACCEPTED


def test_load_missing_ledger_is_empty():
    assert load_ledger(None).entries == {}
    assert load_ledger("/nonexistent/decisions.json").entries == {}
