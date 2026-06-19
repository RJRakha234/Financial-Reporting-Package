from datetime import datetime

from mailflow import save_message
from mailflow.config import Job, SmtpConfig
from mailflow.message import build_context, build_message, render


def smtp():
    return SmtpConfig(
        host="h", port=25, security="none", username="bot@corp.com",
        password_env="X", from_addr="reports@corp.com",
    )


def test_render_substitutes_and_preserves_unknown():
    ctx = {"date": "2026-06-19", "name": "Finance"}
    assert render("Report {date} for {name}", ctx) == "Report 2026-06-19 for Finance"
    # Unknown placeholders and literal braces survive untouched.
    assert render("keep {unknown} and {literal}", ctx) == "keep {unknown} and {literal}"


def test_build_context_has_builtins_and_vars():
    job = Job(name="j", to=["a@e.com"], subject="s", variables={"dept": "Fin"})
    ctx = build_context(job, datetime(2026, 6, 19, 8, 30))
    assert ctx["date"] == "2026-06-19"
    assert ctx["time"] == "08:30"
    assert ctx["job"] == "j"
    assert ctx["dept"] == "Fin"


def test_build_message_headers_and_body():
    job = Job(
        name="weekly",
        to=["a@e.com", "b@e.com"],
        cc=["c@e.com"],
        subject="Report {date}",
        body="Hi {dept}",
        variables={"dept": "Finance"},
    )
    msg = build_message(job, smtp(), datetime(2026, 6, 19, 8, 0))
    assert msg["From"] == "reports@corp.com"
    assert msg["To"] == "a@e.com, b@e.com"
    assert msg["Cc"] == "c@e.com"
    assert msg["Subject"] == "Report 2026-06-19"
    assert msg["Message-ID"].endswith("@corp.com>")
    assert "Hi Finance" in msg.get_content()


def test_build_message_with_attachment_and_html(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    job = Job(
        name="j",
        to=["a@e.com"],
        subject="s",
        body="text",
        html_body="<p>html {job}</p>",
        attachments=[str(f)],
    )
    msg = build_message(job, smtp())
    assert msg.is_multipart()
    payloads = [p.get_filename() for p in msg.iter_attachments()]
    assert "report.pdf" in payloads


def test_save_message_writes_eml(tmp_path):
    job = Job(name="weekly job", to=["a@e.com"], subject="hi", body="b")
    msg = build_message(job, smtp())
    path = save_message(msg, tmp_path, job.name)
    assert path.exists()
    assert path.suffix == ".eml"
    assert b"Subject: hi" in path.read_bytes()
