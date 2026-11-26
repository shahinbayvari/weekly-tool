"""missing_submitters and the /missing page, at a fixed now (Europe/Amsterdam, Mon 09:00 - Fri 17:00)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import CsrfClient
from sqlalchemy import text

from app.config import Settings
from app.items import add_item, delete_item
from app.main import app
from app.missing import missing_query, missing_submitters
from app.pages.submit import get_now, get_settings
from app.users import create_user

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
PW = "correct horse"
OPEN = datetime(2026, 11, 25, 12, 0, tzinfo=AMS)
LAST = datetime(2026, 11, 18, 12, 0, tzinfo=AMS)
CLOSED = datetime(2026, 11, 28, 12, 0, tzinfo=AMS)
WEEK = "2026-11-23"
PREV = "2026-11-16"


@pytest.fixture
def users(db_engine):
    return {
        "owner": create_user("owner@example.com", PW, "owner", engine=db_engine),
        "bob": create_user("bob@example.com", PW, "member", engine=db_engine),
        "ann": create_user("ann@example.com", PW, "member", engine=db_engine),
    }


def add(engine, user, now=OPEN, anonymous=False):
    return add_item(user.id, now, "start", "some text", anonymous, engine=engine, settings=SETTINGS).id


def emails(engine, week=WEEK):
    return [m.email for m in missing_submitters(week, engine=engine)]


def test_everyone_missing_sorted_by_email(db_engine, users):
    assert emails(db_engine) == ["ann@example.com", "bob@example.com", "owner@example.com"]


def test_named_item_removes_user(db_engine, users):
    add(db_engine, users["ann"])
    assert emails(db_engine) == ["bob@example.com", "owner@example.com"]


def test_anonymous_item_removes_user(db_engine, users):
    add(db_engine, users["ann"], anonymous=True)
    assert emails(db_engine) == ["bob@example.com", "owner@example.com"]


def test_add_then_delete_puts_user_back(db_engine, users):
    item_id = add(db_engine, users["ann"])
    assert "ann@example.com" not in emails(db_engine)
    delete_item(item_id, users["ann"].id, OPEN, engine=db_engine, settings=SETTINGS)
    assert "ann@example.com" in emails(db_engine)


def test_other_weeks_do_not_count(db_engine, users):
    add(db_engine, users["ann"], now=LAST)
    assert "ann@example.com" in emails(db_engine)
    assert "ann@example.com" not in emails(db_engine, PREV)


def test_pending_invite_not_listed(db_engine, users):
    with db_engine.begin() as c:
        c.execute(
            text("INSERT INTO invites (email, token_hash, invited_by) VALUES ('new@example.com', 'h', :o)"),
            {"o": users["owner"].id},
        )
    assert "new@example.com" not in emails(db_engine)


def test_everyone_submitted_is_empty(db_engine, users):
    for u in users.values():
        add(db_engine, u)
    assert missing_submitters(WEEK, engine=db_engine) == []


def test_only_owner_no_items(db_engine):
    create_user("owner@example.com", PW, "owner", engine=db_engine)
    assert emails(db_engine) == ["owner@example.com"]


def test_result_identical_when_anonymous_flag_flipped(db_engine, users):
    add(db_engine, users["ann"])
    add(db_engine, users["bob"], anonymous=True)
    before = missing_submitters(WEEK, engine=db_engine)
    with db_engine.begin() as c:
        c.execute(text("UPDATE feedback_items SET anonymous = 1 - anonymous"))
    assert missing_submitters(WEEK, engine=db_engine) == before
    with db_engine.begin() as c:
        c.execute(text("UPDATE feedback_items SET anonymous = 1"))
    assert missing_submitters(WEEK, engine=db_engine) == before


def test_query_never_mentions_anonymous():
    assert "anonymous" not in str(missing_query(WEEK)).lower()


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


def client_for(email):
    c = CsrfClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": email, "password": PW}).status_code == 303
    return c


def test_page_signed_out_redirects(db_engine, users, clock):
    r = CsrfClient(app, follow_redirects=False).get("/missing")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_page_member_forbidden(db_engine, users, clock):
    assert client_for("ann@example.com").get("/missing").status_code == 403


def test_page_lists_missing_with_deadline(db_engine, users, clock):
    add(db_engine, users["ann"])
    add(db_engine, users["owner"])
    add(db_engine, create_user("cy@example.com", PW, "member", engine=db_engine))
    r = client_for("owner@example.com").get("/missing")
    assert r.status_code == 200
    html = r.text
    assert "bob@example.com" in html
    assert "ann@example.com" not in html
    assert "cy@example.com" not in html
    assert "Friday 2026-11-27 17:00 CET (Europe/Amsterdam)" in html
    assert "Everyone has submitted" not in html


def test_page_everyone_submitted(db_engine, users, clock):
    for u in users.values():
        add(db_engine, u, anonymous=True)
    html = client_for("owner@example.com").get("/missing").text
    assert "Everyone has submitted" in html
    assert "<li>" not in html


def test_page_closed_shows_no_list_and_no_query(db_engine, users, clock, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("query must not run")

    monkeypatch.setattr("app.pages.missing.visible_missing", boom)
    clock.now = CLOSED
    html = client_for("owner@example.com").get("/missing").text
    assert "The window is closed" in html
    assert "bob@example.com" not in html and "<li>" not in html


def test_page_shows_no_counts_or_item_data(db_engine, users, clock):
    add_item(users["ann"].id, OPEN, "start", "secret text", True, engine=db_engine, settings=SETTINGS)
    add_item(users["bob"].id, OPEN, "stop", "named text", False, engine=db_engine, settings=SETTINGS)
    html = client_for("owner@example.com").get("/missing").text
    assert "secret text" not in html and "named text" not in html
    assert "anonymous" not in html.lower()
    assert "data-" not in html
    assert "Signed in as owner" in html  # the header shows the viewer, by display name
