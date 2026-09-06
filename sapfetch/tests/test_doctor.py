from sapfetch.config import from_dict
from sapfetch.doctor import run_doctor

CFG = from_dict({
    "portal": {"base_url": "https://example/irj/portal/reports"},
    "reports": [
        {"name": "R", "open_path": ["A", "B"]},
    ],
})


def test_doctor_runs_without_raising_and_checks_python():
    checks = run_doctor(CFG, check_portal=False)
    names = {c.name: c for c in checks}
    # Python version check is pure-stdlib and must be present + truthful.
    assert names["Python >= 3.10"].ok is True
    # Doctor must never raise even if playwright/browser are absent.
    assert "browser launches" in names
    assert "saved SSO session" in names


def test_doctor_reports_missing_session():
    checks = run_doctor(CFG, check_portal=False)
    session_check = next(c for c in checks if c.name == "saved SSO session")
    # No session file in the test env -> reported as not ok, with guidance.
    assert session_check.ok is False
    assert "login" in session_check.detail
