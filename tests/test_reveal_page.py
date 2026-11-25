"""The reveal page (/reveal) at fixed times, via dependency overrides."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import CsrfClient, strip_csrf
from sqlalchemy import text

from app.config import Settings
from app.items import add_item
from app.main import app
from app.pages.submit import get_now, get_settings
from app.users import create_user

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
PW = "correct horse"
LAST = datetime(2026, 11, 18, 12, 0, tzinfo=AMS)  # open, week 2026-11-16
OPEN = datetime(2026, 11, 25, 12, 0, tzinfo=AMS)  # open, week 2026-11-23
CLOSED = datetime(2026, 11, 28, 12, 0, tzinfo=AMS)  # Saturday after week 2026-11-23


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


@pytest.fixture
def users(db_engine):
    made = {
        "owner": create_user("owner@example.com", PW, "owner", engine=db_engine),
        "ann": create_user("ann@example.com", PW, "member", engine=db_engine),
        "bob": create_user("bob@example.com", PW, "member", engine=db_engine),
    }
    set_installed(db_engine, "2026-01-01 00:00:00")
    return made


def set_installed(engine, stamp):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET created_at = :s"), {"s": stamp})


def sign_in(client, email):
    assert client.post("/login", data={"email": email, "password": PW}).status_code == 303


@pytest.fixture
def client(db_engine, clock, users):
    with CsrfClient(app, follow_redirects=False) as c:
        sign_in(c, "ann@example.com")
        yield c


def seed(engine, user, text_, category="start", anonymous=False, now=LAST):
    add_item(user.id, now, category, text_, anonymous, engine=engine, settings=SETTINGS)


def column(html, name):
    m = re.search(rf'<section id="column-{name}">(.*?)</section>', html, re.S)
    assert m, name
    return m.group(1)


def get(client):
    r = client.get("/reveal")
    assert r.status_code == 200
    return strip_csrf(r.text)


def test_requires_sign_in(db_engine, clock, users):
    with CsrfClient(app, follow_redirects=False) as c:
        r = c.get("/reveal")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_owner_and_members_see_same_results(db_engine, clock, users):
    seed(db_engine, users["ann"], "shared text")
    pages = []
    for email in ("owner@example.com", "ann@example.com", "bob@example.com"):
        with CsrfClient(app, follow_redirects=False) as c:
            sign_in(c, email)
            pages.append(re.sub(r"Signed in as \S+", "", get(c)))
    assert pages[0] == pages[1] == pages[2]
    assert "shared text" in pages[0]


def test_closed_window_shows_current_week(client, clock, db_engine, users):
    seed(db_engine, users["ann"], "open item", now=OPEN)
    seed(db_engine, users["bob"], "stop item", "stop", now=OPEN)
    clock.now = CLOSED
    html = get(client)
    assert "Week of 23 Nov 2026" in html
    assert "Results are hidden" not in html
    assert "open item" in column(html, "start")
    assert "stop item" in column(html, "stop")
    assert "No items" in column(html, "continue")


def test_three_columns_in_order_with_all_items(client, db_engine, users):
    seed(db_engine, users["ann"], "a1")
    seed(db_engine, users["bob"], "b1", "stop")
    seed(db_engine, users["ann"], "c1", "continue")
    seed(db_engine, users["bob"], "c2", "continue")
    html = get(client)
    positions = [html.index(f"<h2>{t}</h2>") for t in ("Start", "Stop", "Continue")]
    assert positions == sorted(positions)
    assert "Week of 16 Nov 2026" in html
    assert "a1" in column(html, "start")
    assert "b1" in column(html, "stop")
    assert "c1" in column(html, "continue") and "c2" in column(html, "continue")


def test_named_shows_email_anonymous_shows_nothing_else(client, db_engine, users):
    seed(db_engine, users["ann"], "named one")
    seed(db_engine, users["bob"], "secret one", anonymous=True)
    html = get(client)
    assert "ann@example.com" in html.split("named one")[1].split("</li>")[0]
    li = re.search(r"<li>[^<]*secret one.*?</li>", html, re.S).group(0)
    assert "Anonymous" in li
    assert "@" not in li and "bob" not in li
    assert "<!--" not in html and 'type="hidden"' not in html
    assert "data-" not in html and "title=" not in html
    assert "bob@example.com" not in html


def test_empty_columns_show_no_items(client, db_engine, users):
    html = get(client)
    assert "Week of 16 Nov 2026" in html
    for name in ("start", "stop", "continue"):
        assert "No items" in column(html, name)
    assert "No earlier results" not in html


def test_script_text_is_escaped(client, db_engine, users):
    seed(db_engine, users["ann"], "<script>alert(1)</script>")
    html = get(client)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_no_reactions_history_or_action_items(client, db_engine, users):
    seed(db_engine, users["ann"], "x")
    html = get(client)
    assert "<button" not in html.replace('<button type="submit">Sign out</button>', "")
    assert "<form" not in html.replace('<form method="post" action="/logout">', "")
    assert "<a " not in html and "<input" not in html


def test_open_window_message_and_previous_week(client, db_engine, users):
    seed(db_engine, users["ann"], "last week item")
    html = get(client)
    assert "Results are hidden until Friday 2026-11-27 17:00 CET (Europe/Amsterdam)" in html
    assert html.index("Results are hidden") < html.index("Week of 16 Nov 2026")
    assert "last week item" in column(html, "start")


def test_open_window_source_has_nothing_from_current_week(client, db_engine, users):
    seed(db_engine, users["ann"], "CURRENT-WEEK-TEXT", now=OPEN)
    seed(db_engine, users["bob"], "CURRENT-ANON-TEXT", "stop", True, now=OPEN)
    seed(db_engine, users["owner"], "OLD-ITEM")
    html = get(client)
    for forbidden in ("CURRENT-WEEK-TEXT", "CURRENT-ANON-TEXT", "2026-11-23", "23 Nov"):
        assert forbidden not in html
    assert "bob@example.com" not in html
    assert "OLD-ITEM" in html


def test_crossing_the_deadline_changes_the_page(client, clock, db_engine, users):
    seed(db_engine, users["ann"], "this week", now=OPEN)
    clock.now = datetime(2026, 11, 27, 16, 59, tzinfo=AMS)
    before = get(client)
    assert "Results are hidden until" in before and "this week" not in before
    clock.now = datetime(2026, 11, 27, 17, 0, tzinfo=AMS)
    after = get(client)
    assert "Results are hidden" not in after
    assert "Week of 23 Nov 2026" in after and "this week" in after


def test_year_boundary_previous_week(client, clock, db_engine, users):
    seed(db_engine, users["ann"], "old year", now=datetime(2026, 12, 30, 12, 0, tzinfo=AMS))
    clock.now = datetime(2027, 1, 6, 12, 0, tzinfo=AMS)  # open, week 2027-01-04
    html = get(client)
    assert "Week of 28 Dec 2026" in html
    assert "old year" in column(html, "start")


def test_brand_new_install_shows_only_message(client, clock, db_engine, users):
    set_installed(db_engine, "2026-11-24 10:00:00")  # during the current week
    seed(db_engine, users["ann"], "CURRENT-WEEK-TEXT", now=OPEN)
    html = get(client)
    assert "Results are hidden until" in html
    assert "No earlier results" in html
    assert "<h2>" not in html and "No items" not in html and "Week of" not in html
    assert "CURRENT-WEEK-TEXT" not in html


def test_genuine_earlier_week_without_items_shows_three_columns(client, db_engine, users):
    html = get(client)  # installed in January, nothing in the previous week
    assert "No earlier results" not in html
    assert html.count("No items") == 3
