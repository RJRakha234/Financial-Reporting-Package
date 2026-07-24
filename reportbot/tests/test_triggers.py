from reportbot.mail.base import Message
from reportbot.triggers import TriggerRule, matching_rule


def msg(sender="reports@portal.example.com", subject="Report ready", body=""):
    return Message(id="1", sender=sender, subject=subject, body=body)


def test_from_and_subject_match():
    rule = TriggerRule(
        from_contains=["reports@portal.example.com"],
        subject_contains=["Report ready"],
    )
    assert rule.matches(msg())


def test_all_conditions_must_hold():
    rule = TriggerRule(
        from_contains=["reports@portal.example.com"],
        subject_contains=["daily"],
    )
    # Sender matches but subject does not -> no match.
    assert not rule.matches(msg(subject="Report ready"))


def test_case_insensitive():
    rule = TriggerRule(subject_contains=["report READY"])
    assert rule.matches(msg(subject="Your REPORT ready now"))


def test_subject_regex():
    rule = TriggerRule(subject_regex=r"invoice\s+#\d+")
    assert rule.matches(msg(subject="Invoice #4021 attached"))
    assert not rule.matches(msg(subject="Invoice pending"))


def test_body_contains():
    rule = TriggerRule(body_contains=["download your report"])
    assert rule.matches(msg(body="Please Download Your Report here"))
    assert not rule.matches(msg(body="nothing relevant"))


def test_empty_rule_never_matches():
    assert not TriggerRule().matches(msg())


def test_matching_rule_returns_first_hit():
    rules = [
        TriggerRule(name="a", subject_contains=["nope"]),
        TriggerRule(name="b", from_contains=["portal.example.com"]),
        TriggerRule(name="c", from_contains=["portal.example.com"]),
    ]
    hit = matching_rule(rules, msg())
    assert hit is not None and hit.name == "b"


def test_matching_rule_none():
    rules = [TriggerRule(name="a", subject_contains=["nope"])]
    assert matching_rule(rules, msg()) is None
