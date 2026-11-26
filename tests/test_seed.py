"""The dev seed script (app.seed): in-process for most cases, subprocess for exit codes."""

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import seed as seed_module
from app.config import Settings
from app.db import make_engine
from app.main import app
from app.models import FeedbackItem, Project, User
from app.pages.submit import get_now, get_settings
from app.seed import DEMO_PASSWORD, OWNER_EMAIL, SEED_EMAILS, main
from app.users import create_user
from tests.conftest import CsrfClient

ROOT = Path(__file__).resolve().parent.parent
WEEK = "2026-11-16"  # a Monday
OTHER_WEEK = "2026-11-23"
AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Environment for the script: a DB path that does not exist yet, development mode."""
    db = tmp_path / "sub" / "seed.db"
    db.parent.mkdir()
    monkeypatch.setenv("WEEKLY_DB_PATH", str(db))
    monkeypatch.delenv("WEEKLY_ENV", raising=False)
    monkeypatch.delenv("WEEKLY_TIMEZONE", raising=False)
    return db


def counts(db_path):
    engine = make_engine(str(db_path))
    try:
        with Session(engine) as s:
            return tuple(s.scalar(select(func.count()).select_from(m)) for m in (User, Project, FeedbackItem))
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """One seeded database (week WEEK) shared by the read-only tests; one run hashes 6 passwords."""
    db = tmp_path_factory.mktemp("seeded") / "seed.db"
    mp = pytest.MonkeyPatch()
    mp.setenv("WEEKLY_DB_PATH", str(db))
    mp.delenv("WEEKLY_ENV", raising=False)
    mp.setenv("WEEKLY_TIMEZONE", "Europe/Amsterdam")
    assert main(["--week", WEEK]) == 0
    yield db
    mp.undo()


def test_importing_runs_nothing(tmp_path):
    db = tmp_path / "import.db"
    env = {**os.environ, "WEEKLY_DB_PATH": str(db)}
    subprocess.run([sys.executable, "-c", "import app.seed"], env=env, check=True, cwd=ROOT)
    assert not db.exists()


def test_first_run_creates_everything_and_prints(seeded, capsys):
    # prove the output on a fresh run of its own below; here check the data
    engine = make_engine(str(seeded))
    with Session(engine) as s:
        assert s.scalar(select(Project.name).where(Project.id == 1)) == "Demo Project"
        assert s.scalar(select(func.count()).select_from(Project)) == 1
        users = {u.email: u for u in s.scalars(select(User))}
        assert set(users) == set(SEED_EMAILS)
        assert users[OWNER_EMAIL].role == "owner"
        assert all(users[e].role == "member" for e in SEED_EMAILS if e != OWNER_EMAIL)
        assert all(u.password_hash.startswith("$argon2") for u in users.values())
        assert all(DEMO_PASSWORD not in u.password_hash for u in users.values())
        assert {e: u.display_name for e, u in users.items()} == DEMO_NAMES
    engine.dispose()
    assert len(DEMO_PASSWORD) >= 8


DEMO_NAMES = {
    "owner@example.com": "Olivia Owner",
    "member1@example.com": "Alex",
    "member2@example.com": "Bailey",
    "member3@example.com": "Casey",
    "member4@example.com": "Dana",
    "member5@example.com": "Eli",
}


def test_existing_demo_users_keep_their_names_on_rerun(env):
    assert main(["--week", WEEK]) == 0
    engine = make_engine(str(env))
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET display_name = 'Custom' WHERE email = 'member1@example.com'"))
    engine.dispose()
    assert main(["--week", OTHER_WEEK]) == 0
    engine = make_engine(str(env))
    with Session(engine) as s:
        names = dict(s.execute(select(User.email, User.display_name)).all())
    engine.dispose()
    assert names["member1@example.com"] == "Custom"
    assert names["member2@example.com"] == "Bailey"


def test_items_shape(seeded):
    engine = make_engine(str(seeded))
    with Session(engine) as s:
        items = s.scalars(select(FeedbackItem)).all()
        emails = dict(s.execute(select(User.id, User.email)).all())
    engine.dispose()
    assert len(items) >= 12
    assert {i.week for i in items} == {WEEK}
    assert {i.category for i in items} == {"start", "stop", "continue"}
    assert len({i.author_id for i in items}) >= 4
    assert sum(i.anonymous for i in items) >= 3
    assert sum(not i.anonymous for i in items) >= 3
    authors = {emails[i.author_id] for i in items}
    assert set(SEED_EMAILS) - authors  # someone submitted nothing
    for i in items:
        assert i.text == i.text.strip() and 0 < len(i.text) <= 280
        assert not re.search(r"[\n\r]", i.text)


def test_timestamps_inside_window_and_ordered(seeded):
    engine = make_engine(str(seeded))
    with Session(engine) as s:
        rows = s.scalars(select(FeedbackItem).order_by(FeedbackItem.id)).all()
    engine.dispose()
    opens = datetime(2026, 11, 16, 9, 0, tzinfo=AMS).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    closes = datetime(2026, 11, 20, 17, 0, tzinfo=AMS).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    stamps = [r.created_at for r in rows]
    assert all(opens < t < closes for t in stamps)
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert all(r.updated_at == r.created_at for r in rows)


def test_rerun_same_week_changes_nothing(seeded, capsys):
    before = counts(seeded)
    assert before == (6, 1, 14)
    assert main(["--week", WEEK]) == 0
    assert counts(seeded) == before
    assert "0 new items" in capsys.readouterr().out


def test_different_week_adds_only_items_once(seeded):
    assert main(["--week", OTHER_WEEK]) == 0
    assert counts(seeded) == (6, 1, 28)
    assert main(["--week", OTHER_WEEK]) == 0
    assert counts(seeded) == (6, 1, 28)


def test_partially_seeded_state_has_no_duplicates(env):
    migrated = make_engine(str(env))
    from app.db import migrate

    migrate(str(env))
    create_user("member3@example.com", DEMO_PASSWORD, "member", engine=migrated)
    migrated.dispose()
    assert main(["--week", WEEK]) == 0
    assert counts(env) == (6, 1, 14)


def test_first_run_on_missing_db_migrates_and_prints(env, capsys, monkeypatch):
    monkeypatch.setenv("WEEKLY_TIMEZONE", "UTC")
    assert not env.exists()
    assert main(["--week", WEEK]) == 0
    out = capsys.readouterr().out
    assert env.exists()
    assert OWNER_EMAIL in out and DEMO_PASSWORD in out and WEEK in out
    assert "local demos only" in out
    assert counts(env) == (6, 1, 14)


def test_default_week_is_current_week(env, capsys):
    from datetime import UTC
    from app.config import load_settings
    from app.cycle import describe_settings

    before = describe_settings(datetime.now(UTC), load_settings()).week_id
    assert main([]) == 0
    after = describe_settings(datetime.now(UTC), load_settings()).week_id
    out = capsys.readouterr().out
    assert f"Week: {before}" in out or f"Week: {after}" in out


@pytest.mark.parametrize("bad", ["2026-1-1", "2026-02-30", "not-a-date", "2026-11-17", "2026-11-16 ", "", "20261116"])
def test_invalid_week_writes_nothing(env, capsys, bad):
    assert main(["--week", bad]) == 1
    captured = capsys.readouterr()
    assert "Invalid --week" in captured.err and captured.out == ""
    assert not env.exists()


def test_invalid_week_leaves_existing_db_untouched(env):
    from app.db import migrate

    migrate(str(env))
    assert main(["--week", "2026-11-17"]) == 1
    assert counts(env) == (0, 0, 0)


def test_production_refuses_even_on_empty_db(env, monkeypatch, capsys):
    monkeypatch.setenv("WEEKLY_ENV", "production")
    monkeypatch.setenv("WEEKLY_SECRET_KEY", "a-real-secret")
    monkeypatch.setenv("WEEKLY_SETUP_TOKEN", "a" * 16)
    assert main(["--week", WEEK]) == 1
    assert "production" in capsys.readouterr().err
    assert not env.exists()


def test_production_refuses_with_default_secret_too(env, monkeypatch):
    monkeypatch.setenv("WEEKLY_ENV", "production")  # settings would be invalid; still a refusal, nothing created
    assert main(["--week", WEEK]) == 1
    assert not env.exists()


def test_real_user_present_refuses_and_changes_nothing(env, capsys):
    from app.db import migrate

    migrate(str(env))
    engine = make_engine(str(env))
    create_user("real@company.com", "correct horse", "owner", engine=engine)
    engine.dispose()
    assert main(["--week", WEEK]) == 1
    assert "other than the demo users" in capsys.readouterr().err
    assert counts(env) == (1, 0, 0)


def test_real_user_alongside_seed_users_refuses(seeded, tmp_path, monkeypatch):
    import shutil

    copy = tmp_path / "copy.db"
    shutil.copy(seeded, copy)
    monkeypatch.setenv("WEEKLY_DB_PATH", str(copy))
    engine = make_engine(str(copy))
    create_user("real@company.com", "correct horse", "member", engine=engine)
    engine.dispose()
    before = counts(copy)
    assert main(["--week", OTHER_WEEK]) == 1
    assert counts(copy) == before


def test_signing_in_as_seeded_owner_and_reveal(seeded):
    now = datetime(2026, 11, 21, 12, 0, tzinfo=AMS)  # Saturday after the week's deadline
    app.dependency_overrides[get_now] = lambda: now
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    try:
        with CsrfClient(app, follow_redirects=False) as c:
            r = c.post("/login", data={"email": OWNER_EMAIL, "password": DEMO_PASSWORD})
            assert r.status_code == 303
            r = c.get("/reveal")
            assert r.status_code == 200
            html = r.text
            body = re.sub(r"<header.*?</header>", "", html, flags=re.S)
            assert "Week of 16 Nov 2026" in body
            for _, _, anonymous, item_text in seed_module.SAMPLE_ITEMS:
                assert item_text in body
            anon_texts = [t for _, _, a, t in seed_module.SAMPLE_ITEMS if a]
            for t in anon_texts:
                li = re.search(r"<li>[^<]*" + re.escape(t) + r".*?</li>", body, re.S).group(0)
                assert "Anonymous" in li and "@" not in li
            # no anonymous author appears next to an anonymous item
            assert body.count("Anonymous") >= len(anon_texts)
            # named items show the seed names; no email anywhere on the page
            for _, _, anonymous, item_text in seed_module.SAMPLE_ITEMS:
                if not anonymous:
                    li = re.search(r"<li>[^<]*" + re.escape(item_text) + r".*?</li>", body, re.S).group(0)
                    assert any(name in li for name in DEMO_NAMES.values())
            for email in SEED_EMAILS:
                assert email not in html
            assert "Signed in as Olivia Owner" in html
    finally:
        app.dependency_overrides.pop(get_now, None)
        app.dependency_overrides.pop(get_settings, None)


def run_cli(db, *args, **extra_env):
    env = {k: v for k, v in os.environ.items() if k != "WEEKLY_ENV"}
    env.update({"WEEKLY_DB_PATH": str(db), **extra_env})
    return subprocess.run([sys.executable, "-m", "app.seed", *args], env=env, cwd=ROOT, capture_output=True, text=True)


def test_cli_exit_codes(tmp_path):
    db = tmp_path / "cli" / "cli.db"
    db.parent.mkdir()
    bad = run_cli(db, "--week", "2026-1-1")
    assert bad.returncode != 0 and "Invalid --week" in bad.stderr and not db.exists()

    prod = run_cli(db, "--week", WEEK, WEEKLY_ENV="production", WEEKLY_SECRET_KEY="a-real-secret", WEEKLY_SETUP_TOKEN="a" * 16)
    assert prod.returncode != 0 and "production" in prod.stderr and not db.exists()

    first = run_cli(db, "--week", WEEK)
    assert first.returncode == 0, first.stderr
    assert OWNER_EMAIL in first.stdout and DEMO_PASSWORD in first.stdout and WEEK in first.stdout
    assert counts(db) == (6, 1, 14)

    again = run_cli(db, "--week", WEEK)
    assert again.returncode == 0, again.stderr
    assert counts(db) == (6, 1, 14)

    engine = make_engine(str(db))
    create_user("real@company.com", "correct horse", "member", engine=engine)
    engine.dispose()
    refused = run_cli(db, "--week", OTHER_WEEK)
    assert refused.returncode != 0 and refused.stderr
    assert counts(db) == (7, 1, 14)
