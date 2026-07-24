from reportbot.config import Config


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("PORTAL_PASS", "s3cret")
    cfg = Config.from_dict(
        {
            "portal": {"password": "${PORTAL_PASS}", "login_url": "http://x/login"}
        }
    )
    assert cfg.portal.password == "s3cret"
    assert cfg.portal.login_url == "http://x/login"


def test_missing_env_becomes_empty(monkeypatch):
    monkeypatch.delenv("NOT_SET", raising=False)
    cfg = Config.from_dict({"mail": {"password": "${NOT_SET}"}})
    assert cfg.mail.password == ""


def test_defaults():
    cfg = Config.from_dict({})
    assert cfg.poll_interval_seconds == 60
    assert cfg.mail.backend == "graph"
    assert cfg.mail.folder == "Inbox"
    assert cfg.triggers == []


def test_triggers_parsed():
    cfg = Config.from_dict(
        {
            "triggers": [
                {
                    "name": "daily",
                    "from_contains": "reports@x.com",
                    "subject_contains": ["ready", "done"],
                }
            ]
        }
    )
    assert len(cfg.triggers) == 1
    rule = cfg.triggers[0]
    assert rule.name == "daily"
    assert rule.from_contains == ["reports@x.com"]
    assert rule.subject_contains == ["ready", "done"]


def test_selectors_merge_with_defaults():
    cfg = Config.from_dict({"portal": {"selectors": {"download": "#dl"}}})
    # Overridden key applies; unspecified defaults remain.
    assert cfg.portal.selectors["download"] == "#dl"
    assert cfg.portal.selectors["username"] == "#username"


def test_backend_selection():
    cfg = Config.from_dict({"mail": {"backend": "imap", "port": 993}})
    assert cfg.mail.backend == "imap"
    assert cfg.mail.port == 993
