"""visible_missing and the /missing page safeguard, at a fixed now (Europe/Amsterdam, Mon 09:00 - Fri 17:00)."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import CsrfClient
from sqlalchemy import text

from app.config import Settings
from app.items import add_item
from app.main import app
from app.missing import MIN_SUBMITTERS_FOR_NAMES, MissingCount, MissingNames, visible_missing
from app.pages.submit import get_now, get_settings
from app.users import create_user

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
PW = "correct horse"
OPEN = datetime(2026, 11, 25, 12, 0, tzinfo=AMS)
CLOSED = datetime(2026, 11, 28, 12, 0, tzinfo=AMS)
WEEK = "2026-11-23"
ROOT = Path(__file__).resolve().parent.parent


def team(engine, size):
    users = [create_user("owner@example.com", PW, "owner", engine=engine)]
    for i in range(1, size):
        users.append(create_user(f"m{i}@example.com", PW, "member", engine=engine))
    return users


def submit(engine, users, anonymous=False):
    for u in users:
        add_item(u.id, OPEN, "start", "text", anonymous, engine=engine, settings=SETTINGS)


def vm(engine):
    return visible_missing(WEEK, engine=engine)


def names(result):
    assert isinstance(result, MissingNames)
    return sorted(m.email for m in result.people)


def test_constant_is_three():
    assert MIN_SUBMITTERS_FOR_NAMES == 3


def test_s0_full_list(db_engine):
    team(db_engine, 5)
    assert len(names(vm(db_engine))) == 5


def test_team_of_two(db_engine):
    u = team(db_engine, 2)
    assert names(vm(db_engine)) == ["m1@example.com", "owner@example.com"]
    submit(db_engine, u[:1])
    assert vm(db_engine) == MissingCount(1)
    submit(db_engine, u[1:])
    assert vm(db_engine) == MissingNames(())


def test_team_of_three(db_engine):
    u = team(db_engine, 3)
    assert len(names(vm(db_engine))) == 3
    submit(db_engine, u[:1])
    assert vm(db_engine) == MissingCount(2)
    submit(db_engine, u[1:2])
    assert vm(db_engine) == MissingCount(1)
    submit(db_engine, u[2:])
    assert vm(db_engine) == MissingNames(())


def test_team_of_five_three_submitters_shows_names(db_engine):
    u = team(db_engine, 5)
    submit(db_engine, u[:3])
    assert names(vm(db_engine)) == ["m3@example.com", "m4@example.com"]


def test_team_of_five_two_submitters_count_only(db_engine):
    u = team(db_engine, 5)
    submit(db_engine, u[:2])
    assert vm(db_engine) == MissingCount(3)


def test_boundary_s2_vs_s3(db_engine):
    u = team(db_engine, 6)
    submit(db_engine, u[:2])
    assert isinstance(vm(db_engine), MissingCount)
    submit(db_engine, u[2:3])
    assert isinstance(vm(db_engine), MissingNames)


def test_count_form_has_no_names():
    assert list(MissingCount.__dataclass_fields__) == ["count"]
    with pytest.raises(Exception):
        MissingCount(1).count = 2


def test_anonymous_and_named_identical(db_engine):
    u = team(db_engine, 5)
    submit(db_engine, u[:2])
    named = vm(db_engine)
    with db_engine.begin() as c:
        c.execute(text("UPDATE feedback_items SET anonymous = 1"))
    assert vm(db_engine) == named
    submit(db_engine, u[2:3], anonymous=True)
    anon3 = vm(db_engine)
    with db_engine.begin() as c:
        c.execute(text("UPDATE feedback_items SET anonymous = 0"))
    assert vm(db_engine) == anon3


# page

class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    c = Clock(OPEN)
    app.dependency_overrides[get_now] = c
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield c
    app.dependency_overrides.pop(get_now, None)
    app.dependency_overrides.pop(get_settings, None)


def page(email="owner@example.com"):
    c = CsrfClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": email, "password": PW}).status_code == 303
    r = c.get("/missing")
    assert r.status_code == 200
    return r.text


def test_page_count_plural(db_engine, clock):
    u = team(db_engine, 5)
    submit(db_engine, u[:2])
    html = page()
    assert "3 people have not submitted" in html
    for e in ("m2@example.com", "m3@example.com", "m4@example.com"):
        assert e not in html
    assert "<li>" not in html and "Everyone has submitted" not in html


def test_page_count_singular_no_names(db_engine, clock):
    u = team(db_engine, 2)
    submit(db_engine, u[:1])
    html = page()
    assert "1 person has not submitted" in html
    assert "m1@example.com" not in html
    assert "<li>" not in html


def test_page_count_two_people(db_engine, clock):
    u = team(db_engine, 3)
    submit(db_engine, u[:1])
    html = page()
    assert "2 people have not submitted" in html
    assert "m1@example.com" not in html and "m2@example.com" not in html


def test_page_names_at_three(db_engine, clock):
    u = team(db_engine, 5)
    submit(db_engine, u[:3])
    html = page()
    assert "m3@example.com" in html and "m4@example.com" in html
    assert "have not submitted" not in html


def test_page_nobody_submitted_shows_all(db_engine, clock):
    team(db_engine, 3)
    html = page()
    assert "m1@example.com" in html and "m2@example.com" in html
    assert "have not submitted" not in html


def test_page_everyone_submitted(db_engine, clock):
    u = team(db_engine, 2)
    submit(db_engine, u, anonymous=True)
    html = page()
    assert "Everyone has submitted" in html and "have not submitted" not in html


def test_page_closed_no_query(db_engine, clock, monkeypatch):
    team(db_engine, 3)

    def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr("app.pages.missing.visible_missing", boom)
    clock.now = CLOSED
    html = page()
    assert "The window is closed" in html and "<li>" not in html


def test_no_route_or_template_uses_missing_submitters():
    for folder in ("app/pages", "app/templates"):
        for f in (ROOT / folder).rglob("*"):
            if f.is_file() and f.suffix in {".py", ".html"}:
                assert "missing_submitters" not in f.read_text(encoding="utf-8"), f
