"""The submission page (/submit) and its HTMX fragments, at a fixed time."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import CsrfClient
from sqlalchemy import text

from app.config import Settings
from app.items import add_item, list_items
from app.main import app
from app.pages.submit import get_now, get_settings
from app.users import create_user

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
MID = datetime(2026, 1, 7, 12, 0, tzinfo=AMS)  # open, week 2026-01-05
CLOSED = datetime(2026, 1, 10, 12, 0, tzinfo=AMS)  # Saturday, closed
WEEK = "2026-01-05"
PW = "correct horse"
HX = {"HX-Request": "true"}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    c = Clock(MID)
    app.dependency_overrides[get_now] = c
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield c
    app.dependency_overrides.pop(get_now, None)
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def ann(db_engine):
    return create_user("ann@example.com", PW, "member", engine=db_engine)


@pytest.fixture
def bob(db_engine):
    return create_user("bob@example.com", PW, "member", engine=db_engine)


def sign_in(client, email="ann@example.com"):
    assert client.post("/login", data={"email": email, "password": PW}).status_code == 303


@pytest.fixture
def client(db_engine, clock, ann, bob):
    with CsrfClient(app, follow_redirects=False) as c:
        sign_in(c)
        yield c


def rows(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM feedback_items ORDER BY id")).all()


def seed(engine, user, text_="hello", category="start", anonymous=False, now=MID):
    return add_item(user.id, now, category, text_, anonymous, engine=engine, settings=SETTINGS)


def section(html, name):
    m = re.search(rf'<section id="section-{name}">(.*?)</section>', html, re.S)
    assert m, name
    return m.group(1)


# --- page ---------------------------------------------------------------


def test_requires_login(db_engine, clock, ann):
    with CsrfClient(app, follow_redirects=False) as c:
        r = c.get("/submit")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fsubmit"


def test_three_sections_with_add_forms_and_nothing_yet(client):
    html = client.get("/submit").text
    for name, title in (("start", "Start"), ("stop", "Stop"), ("continue", "Continue")):
        s = section(html, name)
        assert f"<h2>{title}</h2>" in s
        assert f'hx-post="/submit/{name}/items"' in s
        assert 'name="text"' in s and 'name="anonymous"' in s and "Anonymous" in s
        assert "Nothing yet" in s


def test_shows_only_own_items_for_current_week(client, db_engine, ann, bob):
    seed(db_engine, ann, "mine", "stop")
    seed(db_engine, bob, "theirs", "stop")
    seed(db_engine, ann, "old week", "stop", now=datetime(2025, 12, 31, 12, 0, tzinfo=AMS))
    html = client.get("/submit").text
    assert "mine" in section(html, "stop")
    assert "Nothing yet" not in section(html, "stop")
    assert "theirs" not in html and "old week" not in html


def test_owner_sees_own_items_page(db_engine, clock, ann):
    owner = create_user("own@example.com", PW, "owner", engine=db_engine)
    seed(db_engine, owner, "owner item", "continue")
    seed(db_engine, ann, "ann item", "continue")
    with CsrfClient(app, follow_redirects=False) as c:
        sign_in(c, "own@example.com")
        html = c.get("/submit").text
    assert "owner item" in html and "ann item" not in html


def test_item_has_text_edit_delete_and_anonymous_state(client, db_engine, ann):
    seed(db_engine, ann, "plain", "start")
    seed(db_engine, ann, "secret", "start", anonymous=True)
    s = section(client.get("/submit").text, "start")
    assert "Edit</button>" in s and "Delete</button>" in s
    assert "Anonymous: off" in s and "Anonymous: on" in s


def test_open_page_shows_deadline_with_timezone(client):
    html = client.get("/submit").text
    assert "Deadline: Friday 2026-01-09 17:00 CET (Europe/Amsterdam)" in html


def test_closed_page_message_and_no_forms_or_buttons(client, clock, db_engine, ann):
    seed(db_engine, ann, "kept", "start", now=MID)
    clock.now = CLOSED
    html = client.get("/submit").text
    assert "The window is closed. The next one opens Monday 2026-01-12 09:00 CET (Europe/Amsterdam)" in html
    assert "last deadline was Friday 2026-01-09 17:00 CET (Europe/Amsterdam)" in html
    assert 'id="add-' not in html and 'hx-post="/submit' not in html
    assert "<button" not in html.replace('<button type="submit">Sign out</button>', "")
    assert "kept" in html  # read-only


def test_escaped_script_text(client, db_engine, ann):
    r = client.post("/submit/start/items", data={"text": "<script>alert(1)</script>"}, headers=HX)
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    page = client.get("/submit").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    edit = client.get(f"/submit/items/{rows(db_engine)[0].id}/edit", headers=HX)
    assert "<script>alert(1)</script>" not in edit.text


def test_page_references_local_htmx_only(client):
    html = client.get("/submit").text
    assert '<script src="/static/htmx.min.js"></script>' in html
    assert re.findall(r'<script[^>]*\ssrc="([^"]*)"', html) == ["/static/htmx.min.js"]
    assert "http://" not in html and "https://" not in html
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200 and "htmx" in r.text and len(r.content) > 10_000


# --- add ----------------------------------------------------------------


def test_add_puts_item_in_section_and_clears_field(client, db_engine, ann):
    r = client.post("/submit/stop/items", data={"text": "  new thing "}, headers=HX)
    assert r.status_code == 200
    assert 'id="add-stop"' in r.text and 'value=""' in r.text
    assert 'hx-swap-oob="true"' in r.text and 'id="list-stop"' in r.text
    assert "new thing" in r.text and "Nothing yet" not in r.text
    (item,) = list_items(ann.id, WEEK, engine=db_engine)["stop"]
    assert item.text == "new thing" and item.anonymous is False


def test_add_anonymous_checkbox(client, db_engine, ann):
    client.post("/submit/continue/items", data={"text": "x", "anonymous": "on"}, headers=HX)
    (item,) = list_items(ann.id, WEEK, engine=db_engine)["continue"]
    assert item.anonymous is True


@pytest.mark.parametrize(
    "bad,message",
    [("", "must not be empty"), ("   ", "must not be empty"), ("x" * 281, "at most 280"), ("a\nb", "single line")],
)
def test_add_invalid_shows_error_keeps_text_changes_nothing(client, db_engine, bad, message):
    r = client.post("/submit/start/items", data={"text": bad, "anonymous": "on"}, headers=HX)
    assert r.status_code == 200
    assert message in r.text and 'class="error"' in r.text
    assert 'id="list-start"' not in r.text  # list untouched
    assert " checked" in r.text
    if bad.strip():
        assert bad.split("\n")[0] in r.text
    assert rows(db_engine) == []


def test_add_unknown_category_404(client, db_engine):
    assert client.post("/submit/bogus/items", data={"text": "x"}, headers=HX).status_code == 404
    assert rows(db_engine) == []


# --- edit ---------------------------------------------------------------


def test_edit_form_save_and_cancel(client, db_engine, ann):
    item = seed(db_engine, ann, "orig")
    form = client.get(f"/submit/items/{item.id}/edit", headers=HX).text
    assert 'value="orig"' in form and "Save</button>" in form and "Cancel</button>" in form
    cancel = client.get(f"/submit/items/{item.id}", headers=HX).text
    assert "orig" in cancel and "Edit</button>" in cancel
    assert rows(db_engine)[0].text == "orig"
    r = client.post(f"/submit/items/{item.id}/edit", data={"text": "changed"}, headers=HX)
    assert r.status_code == 200 and "changed" in r.text and "orig" not in r.text
    assert rows(db_engine)[0].text == "changed"


@pytest.mark.parametrize("bad,message", [("", "must not be empty"), ("y" * 281, "at most 280"), ("a\nb", "single line")])
def test_edit_invalid_shows_error_keeps_text_changes_nothing(client, db_engine, ann, bad, message):
    item = seed(db_engine, ann, "orig")
    before = rows(db_engine)
    r = client.post(f"/submit/items/{item.id}/edit", data={"text": bad}, headers=HX)
    assert r.status_code == 200 and message in r.text and "Save</button>" in r.text
    assert rows(db_engine) == before


# --- delete / anonymous -------------------------------------------------


def test_delete_removes_item_and_shows_nothing_yet(client, db_engine, ann):
    item = seed(db_engine, ann, "bye", "stop")
    r = client.post(f"/submit/items/{item.id}/delete", headers=HX)
    assert r.status_code == 200 and 'id="list-stop"' in r.text and "Nothing yet" in r.text and "bye" not in r.text
    assert rows(db_engine) == []


def test_delete_keeps_other_items(client, db_engine, ann):
    a = seed(db_engine, ann, "one", "stop")
    seed(db_engine, ann, "two", "stop")
    r = client.post(f"/submit/items/{a.id}/delete", headers=HX)
    assert "two" in r.text and "one" not in r.text and "Nothing yet" not in r.text


def test_toggle_anonymous_persists_after_refresh(client, db_engine, ann):
    item = seed(db_engine, ann, "t")
    assert "Anonymous: off" in section(client.get("/submit").text, "start")
    r = client.post(f"/submit/items/{item.id}/anonymous", data={"anonymous": "true"}, headers=HX)
    assert r.status_code == 200 and "Anonymous: on" in r.text
    assert "Anonymous: on" in section(client.get("/submit").text, "start")
    r = client.post(f"/submit/items/{item.id}/anonymous", data={"anonymous": "false"}, headers=HX)
    assert "Anonymous: off" in r.text
    assert "Anonymous: off" in section(client.get("/submit").text, "start")


# --- window closes while the page is open -------------------------------


def closed_response_ok(r):
    assert r.status_code == 200  # HTMX swaps 2xx only
    assert r.headers["HX-Reswap"] == "none"  # nothing in the page changes...
    assert 'id="notice"' in r.text and "The window is closed" in r.text  # ...except the notice (out of band)
    assert 'hx-swap-oob="true"' in r.text


def test_window_closed_add_edit_delete_toggle(client, clock, db_engine, ann):
    item = seed(db_engine, ann, "orig")
    before = rows(db_engine)
    clock.now = CLOSED
    closed_response_ok(client.post("/submit/start/items", data={"text": "new"}, headers=HX))
    closed_response_ok(client.post(f"/submit/items/{item.id}/edit", data={"text": "changed"}, headers=HX))
    closed_response_ok(client.get(f"/submit/items/{item.id}/edit", headers=HX))
    closed_response_ok(client.post(f"/submit/items/{item.id}/delete", headers=HX))
    closed_response_ok(client.post(f"/submit/items/{item.id}/anonymous", data={"anonymous": "true"}, headers=HX))
    assert rows(db_engine) == before


def test_success_response_clears_notice(client):
    r = client.post("/submit/start/items", data={"text": "ok"}, headers=HX)
    assert re.search(r'<div id="notice"[^>]*hx-swap-oob="true"></div>', r.text)
    assert "HX-Reswap" not in r.headers


def test_page_has_notice_target(client):
    assert 'id="notice"' in client.get("/submit").text


# --- someone else's item ------------------------------------------------


def test_other_members_item_is_forbidden_and_unchanged(client, db_engine, bob):
    item = seed(db_engine, bob, "bobs", "start")
    before = rows(db_engine)
    r1 = client.post(f"/submit/items/{item.id}/edit", data={"text": "hacked"}, headers=HX)
    r2 = client.post(f"/submit/items/{item.id}/delete", headers=HX)
    r3 = client.post(f"/submit/items/{item.id}/anonymous", data={"anonymous": "true"}, headers=HX)
    assert (r1.status_code, r2.status_code, r3.status_code) == (403, 403, 403)
    assert rows(db_engine) == before


def test_other_members_item_invalid_edit_and_views_hidden(client, db_engine, bob):
    item = seed(db_engine, bob, "bobs", "start")
    before = rows(db_engine)
    assert client.post(f"/submit/items/{item.id}/edit", data={"text": ""}, headers=HX).status_code in (403, 404)
    assert client.get(f"/submit/items/{item.id}/edit", headers=HX).status_code in (403, 404)
    assert client.get(f"/submit/items/{item.id}", headers=HX).status_code in (403, 404)
    assert rows(db_engine) == before


def test_missing_item_404(client):
    assert client.post("/submit/items/999/edit", data={"text": "x"}, headers=HX).status_code == 404
    assert client.post("/submit/items/999/delete", headers=HX).status_code == 404
    assert client.post("/submit/items/999/anonymous", data={"anonymous": "true"}, headers=HX).status_code == 404


def test_writes_require_login(db_engine, clock, ann):
    item = seed(db_engine, ann, "x")
    with CsrfClient(app, follow_redirects=False) as c:
        assert c.post("/submit/start/items", data={"text": "x"}).status_code == 303
        assert c.post(f"/submit/items/{item.id}/delete").status_code == 303
    assert len(rows(db_engine)) == 1


# --- plumbing -----------------------------------------------------------


def test_get_now_is_aware_utc():
    real = get_now()
    assert real.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - real) < timedelta(seconds=5)
