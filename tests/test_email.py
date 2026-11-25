import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import EMAIL_BACKENDS, Settings, load_settings
from app.email import BACKENDS, get_email_sender
from tests.fakes import FakeEmailSender

ROOT = Path(__file__).resolve().parent.parent


def block(to, subject, body):
    return f"--- EMAIL ---\nTo: {to}\nSubject: {subject}\n{body}\n--- END ---\n"


def sender():
    return get_email_sender(Settings())


def test_exact_output(capsys):
    sender().send("a@example.com", "Hello", "Line one\nLine two")
    out = capsys.readouterr()
    assert out.out == block("a@example.com", "Hello", "Line one\nLine two")
    assert out.err == ""


def test_two_sends_two_blocks_in_order(capsys):
    s = sender()
    s.send("a@example.com", "First", "one")
    s.send("b@example.com", "Second", "two")
    assert capsys.readouterr().out == block("a@example.com", "First", "one") + block(
        "b@example.com", "Second", "two"
    )


@pytest.mark.parametrize(
    "body",
    ["", "héllo wörld ✓ 日本語 🎉", "x\n\n\n", "\nleading", "--- END ---\nmore", "tab\tand\r\nCRLF"],
)
def test_body_printed_unchanged(capsys, body):
    sender().send("a@example.com", "S", body)
    assert capsys.readouterr().out == block("a@example.com", "S", body)


@pytest.mark.parametrize("subject", ["", "   "])
def test_empty_and_whitespace_subject_allowed(capsys, subject):
    sender().send("a@example.com", subject, "b")
    assert capsys.readouterr().out == block("a@example.com", subject, "b")


def test_non_ascii_recipient_and_subject(capsys):
    sender().send("jöhn@example.com", "Grüße ✓", "b")
    assert capsys.readouterr().out == block("jöhn@example.com", "Grüße ✓", "b")


BREAKS = ["\n", "\r", "\r\n", " ", " ", "\x0b", "\x0c", "\x85", "\x1c", "\x1d", "\x1e"]


@pytest.mark.parametrize(
    "to, subject",
    [
        ("", "S"),
        ("   ", "S"),
        ("no-at-sign", "S"),
        *[(f"a@example.com{b}Bcc: evil@example.com", "S") for b in BREAKS],
        *[("a@example.com", f"S{b}Bcc: evil@example.com") for b in BREAKS],
        ("a@example.com\n", "S"),
        ("a@example.com", "S\n"),
    ],
)
def test_invalid_input_raises_and_prints_nothing(capsys, to, subject):
    with pytest.raises(ValueError):
        sender().send(to, subject, "body")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_stdout_looked_up_at_call_time(monkeypatch):
    s = sender()
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    s.send("a@example.com", "S", "b")
    assert buf.getvalue() == block("a@example.com", "S", "b")


def test_unencodable_stdout_raises_and_writes_nothing(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict", newline="\n", write_through=True)
    monkeypatch.setattr(sys, "stdout", stream)
    with pytest.raises(UnicodeEncodeError):
        sender().send("a@example.com", "S", "café")
    assert raw.getvalue() == b""
    sender().send("a@example.com", "S", "cafe")
    assert raw.getvalue() == block("a@example.com", "S", "cafe").encode("ascii")


def test_get_email_sender_follows_settings(capsys):
    s = get_email_sender(load_settings({"WEEKLY_EMAIL_BACKEND": "console"}))
    assert isinstance(s, BACKENDS["console"])
    s.send("a@example.com", "S", "b")
    assert capsys.readouterr().out.startswith("--- EMAIL ---\n")


def test_accepted_values_come_from_registry():
    assert set(EMAIL_BACKENDS) == set(BACKENDS)


def test_fake_sender_records_calls():
    fake = FakeEmailSender()
    fake.send("a@example.com", "S1", "b1")
    fake.send("b@example.com", "S2", "b2")
    assert fake.sent == [("a@example.com", "S1", "b1"), ("b@example.com", "S2", "b2")]


def test_callers_do_not_reference_console_class():
    pattern = re.compile(r"ConsoleEmailSender|\bBACKENDS\b")
    skip = {
        ROOT / "app" / "email.py",
        ROOT / "app" / "config.py",  # derives accepted values from the registry
        ROOT / "tests" / "test_email.py",
        ROOT / "tests" / "fakes.py",
    }
    offenders = []
    for folder in ("app", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if path.resolve() in skip:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_stdlib_email_not_shadowed():
    code = (
        "import email, email.message, email.mime.text, app.email, app.main\n"
        "assert '/app/' not in email.__file__.replace(chr(92), '/'), email.__file__\n"
        "import multipart, starlette.formparsers, uvicorn\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
