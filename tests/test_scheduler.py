"""The reminder scheduler, the job function and the `python -m app.reminders` command line."""

import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app.main
import app.reminders as reminders
import app.scheduler as scheduler_module
from app.config import Settings
from app.scheduler import JOB_ID, get_scheduler, run_reminder_job, start_scheduler, stop_scheduler
from app.users import create_user
from tests.fakes import FakeEmailSender

ROOT = Path(__file__).resolve().parent.parent
AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(
    timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00"
)
ENABLED = replace(SETTINGS, scheduler_enabled=True, reminder_interval_minutes=7)
LEAD = datetime(2026, 11, 26, 17, 0, tzinfo=AMS)  # Thursday 17:00, 24h before the deadline


@pytest.fixture(autouse=True)
def clean_scheduler():
    """Make sure no scheduler leaks in or out of a test."""
    for _ in range(5):
        stop_scheduler()
    yield
    for _ in range(5):
        stop_scheduler()
    assert get_scheduler() is None


# --- settings: the test session runs with the scheduler off ---------------------


def test_test_session_has_scheduler_disabled_by_default():
    assert os.environ["WEEKLY_SCHEDULER_ENABLED"] == "false"
    assert app.main.settings.scheduler_enabled is False


def test_app_lifespan_with_scheduler_disabled_starts_nothing():
    with TestClient(app.main.app) as client:
        assert client.get("/health").status_code == 200
        assert get_scheduler() is None


# --- start and stop ---------------------------------------------------------------


def test_disabled_starts_no_scheduler():
    assert start_scheduler(replace(ENABLED, scheduler_enabled=False)) is False
    assert get_scheduler() is None


def test_start_schedules_the_job_with_interval_and_short_first_run():
    before = datetime.now(UTC)
    assert start_scheduler(ENABLED) is True
    sched = get_scheduler()
    assert sched is not None and sched.running
    jobs = sched.get_jobs()
    assert [j.id for j in jobs] == [JOB_ID]
    job = jobs[0]
    assert job.trigger.interval == timedelta(minutes=7)
    assert job.max_instances == 1
    assert job.coalesce is True
    first = job.next_run_time
    assert before <= first <= before + timedelta(seconds=scheduler_module.FIRST_RUN_DELAY_SECONDS + 5)


def test_starting_twice_leaves_exactly_one_scheduler():
    assert start_scheduler(ENABLED) is True
    first = get_scheduler()
    assert start_scheduler(ENABLED) is True
    assert get_scheduler() is first
    assert len(first.get_jobs()) == 1
    stop_scheduler()
    assert get_scheduler() is first and first.running  # one user still active
    stop_scheduler()
    assert get_scheduler() is None and not first.running


def test_creating_the_app_lifespan_twice_leaves_one_scheduler(monkeypatch):
    monkeypatch.setattr(app.main, "settings", ENABLED)
    monkeypatch.setattr(scheduler_module, "FIRST_RUN_DELAY_SECONDS", 3600)  # no run during the test
    with TestClient(app.main.app):
        first = get_scheduler()
        assert first is not None
        with TestClient(app.main.app):
            assert get_scheduler() is first
            assert len(first.get_jobs()) == 1
            threads = [t for t in threading.enumerate() if t.name.startswith("APScheduler")]
            assert len(threads) <= 1
    assert get_scheduler() is None


def test_app_shutdown_stops_the_scheduler(monkeypatch):
    monkeypatch.setattr(app.main, "settings", ENABLED)
    monkeypatch.setattr(scheduler_module, "FIRST_RUN_DELAY_SECONDS", 3600)
    with TestClient(app.main.app):
        sched = get_scheduler()
        assert sched.running
    assert not sched.running
    assert get_scheduler() is None


def test_shutdown_does_not_wait_for_a_running_job(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def slow(**_kw):
        started.set()
        release.wait(30)

    monkeypatch.setattr(scheduler_module, "run_reminder_job", slow)
    monkeypatch.setattr(scheduler_module, "FIRST_RUN_DELAY_SECONDS", 0)
    start_scheduler(ENABLED)
    assert started.wait(5)
    t0 = time.monotonic()
    stop_scheduler()
    assert time.monotonic() - t0 < 2
    release.set()


# --- the job: errors, single flight ---------------------------------------------


def test_job_returns_count_with_fake_sender_and_real_db(db_engine):
    create_user("ann@example.com", "correct horse", "member", engine=db_engine)
    sender = FakeEmailSender()
    assert run_reminder_job(settings=SETTINGS, sender=sender, now=LEAD, engine=db_engine) == 1
    assert [to for to, _, _ in sender.sent] == ["ann@example.com"]
    assert run_reminder_job(settings=SETTINGS, sender=sender, now=LEAD, engine=db_engine) == 0


def test_failing_run_is_logged_without_addresses_and_does_not_raise(monkeypatch, caplog):
    def boom(*_a, **_kw):
        raise RuntimeError("could not reach ann@example.com")

    monkeypatch.setattr(scheduler_module, "send_reminders", boom)
    with caplog.at_level(logging.ERROR, logger="app.scheduler"):
        assert run_reminder_job(settings=SETTINGS, sender=FakeEmailSender(), now=LEAD) is None
    assert "RuntimeError" in caplog.text
    assert "ann@example.com" not in caplog.text
    # the lock was released: the next run works
    monkeypatch.setattr(scheduler_module, "send_reminders", lambda *a, **k: 4)
    assert run_reminder_job(settings=SETTINGS, sender=FakeEmailSender(), now=LEAD) == 4


def test_failing_runs_do_not_stop_the_schedule(monkeypatch):
    calls = []

    def boom(*_a, **_kw):
        calls.append(1)
        raise RuntimeError("nope")

    monkeypatch.setattr(scheduler_module, "send_reminders", boom)
    monkeypatch.setattr(scheduler_module, "FIRST_RUN_DELAY_SECONDS", 0)
    start_scheduler(ENABLED)
    get_scheduler().reschedule_job(JOB_ID, trigger="interval", seconds=1)
    deadline = time.monotonic() + 8
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(calls) >= 2
    assert get_scheduler().running


def test_slow_job_is_skipped_not_run_in_parallel_by_the_scheduler(monkeypatch):
    state = {"running": 0, "max": 0, "calls": 0}
    lock = threading.Lock()

    def slow(**_kw):
        with lock:
            state["running"] += 1
            state["calls"] += 1
            state["max"] = max(state["max"], state["running"])
        time.sleep(3)
        with lock:
            state["running"] -= 1

    monkeypatch.setattr(scheduler_module, "run_reminder_job", slow)
    monkeypatch.setattr(scheduler_module, "FIRST_RUN_DELAY_SECONDS", 0)
    start_scheduler(ENABLED)
    get_scheduler().reschedule_job(JOB_ID, trigger="interval", seconds=1)
    time.sleep(5)
    assert state["max"] == 1
    assert state["calls"] >= 1


class SlowSender(FakeEmailSender):
    def __init__(self):
        super().__init__()
        self.in_send = threading.Event()

    def send(self, to, subject, body):
        self.in_send.set()
        time.sleep(0.3)
        super().send(to, subject, body)


def test_overlapping_direct_calls_are_skipped_and_nobody_gets_two_emails(db_engine):
    for name in ("ann", "bob", "cat"):
        create_user(f"{name}@example.com", "correct horse", "member", engine=db_engine)

    sender = SlowSender()
    results = {}

    def first():
        results["first"] = run_reminder_job(settings=SETTINGS, sender=sender, now=LEAD, engine=db_engine)

    t = threading.Thread(target=first)
    t.start()
    assert sender.in_send.wait(5)
    results["second"] = run_reminder_job(settings=SETTINGS, sender=sender, now=LEAD, engine=db_engine)
    t.join(30)
    assert results == {"first": 3, "second": None}
    assert sorted(to for to, _, _ in sender.sent) == ["ann@example.com", "bob@example.com", "cat@example.com"]


def test_two_threads_calling_the_job_send_each_member_exactly_once(db_engine):
    for name in ("ann", "bob"):
        create_user(f"{name}@example.com", "correct horse", "member", engine=db_engine)

    sender = SlowSender()
    threads = [
        threading.Thread(
            target=run_reminder_job, kwargs=dict(settings=SETTINGS, sender=sender, now=LEAD, engine=db_engine)
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(to for to, _, _ in sender.sent) == ["ann@example.com", "bob@example.com"]


# --- command line -----------------------------------------------------------------


def run_cli(env_extra=None, db_path=None):
    env = {**os.environ, "WEEKLY_SCHEDULER_ENABLED": "true", **(env_extra or {})}
    if db_path is not None:
        env["WEEKLY_DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, "-m", "app.reminders"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )


def test_cli_prints_count_line_and_exits_zero(db_engine):
    proc = run_cli()  # WEEKLY_DB_PATH is the migrated, empty test database (set by db_engine)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines() == ["Reminders sent: 0"]
    assert proc.stderr == ""


def test_importing_and_running_the_cli_starts_no_scheduler_or_server(db_engine):
    code = (
        "import app.reminders, app.scheduler, threading\n"
        "assert app.scheduler.get_scheduler() is None\n"
        "assert threading.active_count() == 1\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "WEEKLY_SCHEDULER_ENABLED": "true"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.strip() == "ok", proc.stderr
    assert run_cli().returncode == 0  # a run with the scheduler enabled still exits by itself


def test_cli_count_line_comes_after_the_emails(monkeypatch, capsys):
    def fake_send(now, sender, settings, **_kw):
        sender.send("ann@example.com", "Subject", "Body")
        return 1

    monkeypatch.setattr(reminders, "send_reminders", fake_send)
    monkeypatch.setattr(reminders, "_check_database", lambda _s: None)
    assert reminders.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "--- EMAIL ---" in lines and "--- END ---" in lines
    assert lines[-1] == "Reminders sent: 1"
    assert lines.index("--- END ---") < len(lines) - 1


def test_cli_failure_goes_to_stderr_with_nonzero_exit(monkeypatch, capsys):
    def boom(*_a, **_kw):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(reminders, "send_reminders", boom)
    monkeypatch.setattr(reminders, "_check_database", lambda _s: None)
    assert reminders.main() == 1
    out = capsys.readouterr()
    assert out.err.startswith("Error:") and "smtp is down" in out.err
    assert out.out == ""


def test_cli_invalid_setting_fails_with_nonzero_exit(db_engine):
    proc = run_cli({"WEEKLY_REMINDER_HOURS_BEFORE": "abc"})
    assert proc.returncode != 0
    assert "WEEKLY_REMINDER_HOURS_BEFORE" in proc.stderr
    assert proc.stdout == ""


def test_cli_database_without_tables_fails_clearly(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    proc = run_cli(db_path=db)
    assert proc.returncode != 0
    assert "missing tables" in proc.stderr
    assert proc.stdout == ""


def test_cli_missing_database_file_fails_without_creating_it(tmp_path):
    db = tmp_path / "nope.db"
    proc = run_cli(db_path=db)
    assert proc.returncode != 0
    assert "not found" in proc.stderr
    assert not db.exists()


# --- a real server process --------------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_uvicorn_process_exits_promptly_with_scheduler_running(db_engine):
    port = free_port()
    env = {**os.environ, "WEEKLY_SCHEDULER_ENABLED": "true", "WEEKLY_REMINDER_INTERVAL_MINUTES": "1"}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            assert proc.poll() is None, "server exited early"
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.2)
        time.sleep(7)  # past the first scheduled run
        assert proc.poll() is None
        t0 = time.monotonic()
        if sys.platform == "win32":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        assert time.monotonic() - t0 < 10
    finally:
        if proc.poll() is None:
            proc.kill()  # only the process this test started
            proc.wait(timeout=10)
