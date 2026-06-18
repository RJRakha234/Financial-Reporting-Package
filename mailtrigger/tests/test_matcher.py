from mailtrigger.mailbox import Attachment, Email
from mailtrigger.matcher import matches


def _mail(**kw):
    base = dict(uid="1", from_addr="reports@vendor.com", subject="Run report now", body="hello", attachments=[])
    base.update(kw)
    return Email(**base)


def test_all_conditions_match():
    trigger = {"from_contains": "vendor.com", "subject_contains": "run report"}
    assert matches(_mail(), trigger)


def test_sender_mismatch():
    trigger = {"from_contains": "other.com"}
    assert not matches(_mail(), trigger)


def test_subject_mismatch():
    trigger = {"subject_contains": "invoice"}
    assert not matches(_mail(), trigger)


def test_empty_trigger_matches_everything():
    assert matches(_mail(), {})


def test_require_attachment():
    trigger = {"require_attachment": True}
    assert not matches(_mail(), trigger)
    assert matches(_mail(attachments=[Attachment("a.pdf", b"x")]), trigger)


def test_case_insensitive():
    trigger = {"subject_contains": "RUN REPORT", "from_contains": "VENDOR"}
    assert matches(_mail(), trigger)
