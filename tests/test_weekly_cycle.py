"""One end-to-end walk through a whole weekly cycle, over HTTP, with a controlled clock.

Only HTTP requests are used, except for the reminder function (called directly with a
fake sender) and the clock (set through the get_now / get_settings overrides).
Nothing is inserted into the database directly and the real clock is never read.
"""

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.auth import get_engine
from app.config import Settings
from app.email import get_email_sender
from app.main import app
from app.pages.submit import get_now, get_settings
from app.reminders import send_reminders
from tests.conftest import CsrfClient, strip_csrf
from tests.fakes import FakeEmailSender

AMS = ZoneInfo("Europe/Amsterdam")
SETUP_TOKEN = "weekly-setup-token-1234"
SETTINGS = Settings(
    timezone="Europe/Amsterdam",
    open_day="monday",
    open_time="09:00",
    close_day="friday",
    close_time="17:00",
    reminder_hours_before=24,
    setup_token=SETUP_TOKEN,
)
OWNER = "owner@example.com"
OWNER_NAME = "Olivia Owner"
ANN = "ann@example.com"
ANN_NAME = "Ann Alpha"
PASSWORD = "correct horse battery"
HX = {"HX-Request": "true"}

# Week of Monday 23 Nov 2026 (Europe/Amsterdam), then the next Monday.
WED = datetime(2026, 11, 25, 10, 0, tzinfo=AMS)
THU_REMINDER = datetime(2026, 11, 26, 17, 0, tzinfo=AMS)  # 24 h before the deadline
THU_EVENING = datetime(2026, 11, 26, 18, 0, tzinfo=AMS)
DEADLINE = datetime(2026, 11, 27, 17, 0, tzinfo=AMS)
NEXT_MONDAY = datetime(2026, 11, 30, 9, 0, tzinfo=AMS)

OWNER_TEXT = "Owner named item: pair on reviews"
ANN_NAMED = "Ann named item: shorter standups"
ANN_ANON = "Ann anonymous item: unclear priorities"
NEW_WEEK_TEXT = "New week item: try demo fridays"
CLOSED = "The window is closed"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def column(html, name):
    m = re.search(rf'<section id="column-{name}">(.*?)</section>', html, re.S)
    assert m, f"no column {name}"
    return m.group(1)


def item_ids(html):
    return [int(i) for i in re.findall(r"/submit/items/(\d+)/edit", html)]


def add_item(client, category, text, anonymous=False):
    data = {"text": text}
    if anonymous:
        data["anonymous"] = "on"
    r = client.post(f"/submit/{category}/items", data=data, headers=HX)
    assert r.status_code == 200
    return r


def new_item_id(client, category, text, anonymous=False):
    r = add_item(client, category, text, anonymous)
    assert text in r.text and CLOSED not in r.text
    return max(item_ids(r.text))


def assert_refused(response, *, text=None):
    """A refused write: 403/404, or the 200 'window is closed' notice that swaps nothing."""
    if response.status_code == 200:
        assert CLOSED in response.text, response.text
        assert response.headers.get("HX-Reswap") == "none"
    else:
        assert response.status_code in (403, 404), response.status_code
    if text:
        assert text not in response.text


def test_weekly_cycle(db_engine, monkeypatch):
    started = time.perf_counter()
    clock = Clock(WED)
    fake = FakeEmailSender()

    def console_must_not_be_used(self, to, subject, body):
        raise AssertionError("the console email sender must not be used")

    # The configured (console) backend class, found without naming it.
    monkeypatch.setattr(type(get_email_sender(SETTINGS)), "send", console_must_not_be_used)
    app.dependency_overrides[get_engine] = lambda: db_engine
    app.dependency_overrides[get_now] = clock
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    try:
        with CsrfClient(app, follow_redirects=False) as owner, CsrfClient(app, follow_redirects=False) as ann:
            walk(owner, ann, clock, fake, db_engine)
    finally:
        for dep in (get_engine, get_now, get_settings):
            app.dependency_overrides.pop(dep, None)
    assert time.perf_counter() - started < 5


def walk(owner, ann, clock, fake, engine):
    # 1. Setup: the owner is created and signed in.
    r = owner.post(
        "/setup",
        data={"project_name": "Team", "email": OWNER, "display_name": OWNER_NAME, "password": PASSWORD,
              "confirm_password": PASSWORD, "setup_token": SETUP_TOKEN},
    )
    assert r.status_code == 303
    page = owner.get("/submit")
    assert page.status_code == 200 and f"Signed in as {OWNER_NAME}" in page.text and OWNER not in page.text
    assert owner.get("/invites").status_code == 200  # owner-only page

    # 2. Invite: the owner invites ann, the link is taken from the response page.
    r = owner.post("/invites", data={"email": ANN})
    assert r.status_code == 200
    link = re.search(r"<code>(.*?)</code>", r.text, re.S).group(1).strip()
    path = "/" + link.split("://", 1)[1].split("/", 1)[1]
    assert path.startswith("/invite/")
    assert ann.get("/submit").status_code == 303  # not signed in yet
    assert ann.get(path).status_code == 200
    r = ann.post(path, data={"display_name": ANN_NAME, "password": PASSWORD, "confirm_password": PASSWORD})
    assert r.status_code == 303
    page = ann.get("/submit")
    assert page.status_code == 200 and f"Signed in as {ANN_NAME}" in page.text and ANN not in page.text
    assert ann.get("/invites").status_code in (403, 404)  # a member, not an owner
    assert ann.get("/missing").status_code in (403, 404)

    # 3. Open window (Wednesday 10:00): the owner submits one named item, ann nothing.
    clock.now = WED
    owner_item = new_item_id(owner, "start", OWNER_TEXT)
    assert OWNER_TEXT in owner.get("/submit").text
    assert OWNER_TEXT not in ann.get("/submit").text
    assert item_ids(ann.get("/submit").text) == []

    # 4. Missing list: team of two, one submitted -> only the count, never ann's email or name.
    r = owner.get("/missing")
    assert "1 person has not submitted" in r.text
    assert ANN not in r.text and ANN_NAME not in r.text

    # 5. Reminder: Thursday 17:00 emails only ann; a second run sends nothing.
    clock.now = THU_REMINDER
    assert send_reminders(THU_REMINDER, fake, SETTINGS, engine=engine) == 1
    assert [to for to, _, _ in fake.sent] == [ANN]
    assert send_reminders(THU_REMINDER, fake, SETTINGS, engine=engine) == 0
    assert [to for to, _, _ in fake.sent] == [ANN]

    # 6. Ann submits a named and an anonymous item; everyone has now submitted.
    clock.now = THU_EVENING
    ann_named = new_item_id(ann, "stop", ANN_NAMED)
    ann_anon = new_item_id(ann, "continue", ANN_ANON, anonymous=True)
    assert "Anonymous: on" in ann.get("/submit").text
    assert "Everyone has submitted" in owner.get("/missing").text

    # 7. Hidden while open. Emails never appear on the reveal page; a name appears only in the
    # header of its own viewer ("Signed in as ..."), so ann's name is absent from the owner's page.
    for client in (owner, ann):
        html = client.get("/reveal").text
        assert "Results are hidden until" in html
        for t in (OWNER_TEXT, ANN_NAMED, ANN_ANON):
            assert t not in html
    owner_html = owner.get("/reveal").text
    assert ANN not in owner_html and ANN_NAME not in owner_html and OWNER not in owner_html
    ann_html = ann.get("/reveal").text
    assert ANN not in ann_html and OWNER not in ann_html
    assert ann_html.count(ANN_NAME) == 1 and f"Signed in as {ANN_NAME}" in ann_html

    # 8. Deadline: one microsecond before, ann can still edit; at 17:00 sharp everything is refused.
    clock.now = DEADLINE - timedelta(microseconds=1)
    edited = ANN_NAMED + " (edited)"
    r = ann.post(f"/submit/items/{ann_named}/edit", data={"text": edited}, headers=HX)
    assert r.status_code == 200 and edited in r.text and CLOSED not in r.text

    clock.now = DEADLINE
    assert_refused(add_item(ann, "start", "Too late item"), text="Too late item")
    assert_refused(
        ann.post(f"/submit/items/{ann_named}/edit", data={"text": "Too late edit"}, headers=HX), text="Too late edit"
    )
    assert_refused(ann.post(f"/submit/items/{ann_named}/delete", headers=HX))
    assert_refused(ann.post(f"/submit/items/{ann_anon}/anonymous", data={"anonymous": "false"}, headers=HX))
    assert_refused(ann.post(f"/submit/items/{ann_named}/anonymous", data={"anonymous": "true"}, headers=HX))
    # Nothing changed (checked through pages, not the database).
    page = ann.get("/submit").text
    assert edited in page and ANN_ANON in page and "Too late" not in page
    assert page.count("Anonymous: on") == 1  # only the anonymous item
    assert page.count("Anonymous: off") == 1  # the named item; two items in total

    # 9. Reveal: after the deadline all three items are in the right columns.
    assert_revealed(owner, edited, viewer=OWNER)
    assert_revealed(ann, edited, viewer=ANN)

    # 10. Next week (Monday 09:00): a new item is accepted, the old week stays under its heading.
    clock.now = NEXT_MONDAY
    new_id = new_item_id(owner, "start", NEW_WEEK_TEXT)
    for client in (owner, ann):
        html = client.get("/reveal").text
        assert "Week of 23 Nov 2026" in html
        assert NEW_WEEK_TEXT not in html
        assert OWNER_TEXT in column(html, "start")
        assert edited in column(html, "stop")
        assert ANN_ANON in column(html, "continue")
    assert NEW_WEEK_TEXT in owner.get("/submit").text
    assert OWNER_TEXT not in owner.get("/submit").text  # /submit shows only the new week

    # 11. Writes to last week's items are still refused in the new week.
    assert_refused(
        ann.post(f"/submit/items/{ann_named}/edit", data={"text": "Old week edit"}, headers=HX), text="Old week edit"
    )
    assert_refused(ann.post(f"/submit/items/{ann_named}/delete", headers=HX))
    assert_refused(ann.post(f"/submit/items/{ann_anon}/anonymous", data={"anonymous": "false"}, headers=HX))
    assert_refused(
        owner.post(f"/submit/items/{owner_item}/edit", data={"text": "Old week edit"}, headers=HX),
        text="Old week edit",
    )
    assert_refused(owner.post(f"/submit/items/{owner_item}/delete", headers=HX))
    assert_refused(owner.post(f"/submit/items/{owner_item}/anonymous", data={"anonymous": "true"}, headers=HX))
    for client in (owner, ann):
        html = client.get("/reveal").text
        assert OWNER_TEXT in column(html, "start") and "Old week edit" not in html
        assert edited in column(html, "stop") and ANN_ANON in column(html, "continue")
    assert item_ids(owner.get("/submit").text) == [new_id]

    # Only the one reminder was ever emailed.
    assert [to for to, _, _ in fake.sent] == [ANN]


def assert_revealed(client, named, *, viewer):
    """`viewer` is the viewer's email; the page shows names only, never an email."""
    html = client.get("/reveal").text
    assert "Week of 23 Nov 2026" in html and "Results are hidden" not in html
    start, stop, cont = column(html, "start"), column(html, "stop"), column(html, "continue")
    assert OWNER_TEXT in start and OWNER_NAME in start
    assert named in stop and ANN_NAME in stop
    assert ANN_ANON in cont and "Anonymous" in cont and ANN_NAME not in cont and OWNER_NAME not in cont
    assert ANN_NAME not in start and OWNER_NAME not in stop
    assert ANN not in html and OWNER not in html  # no email anywhere, not even the viewer's own
    # A name appears next to its named item; in the viewer's own view it also appears in the header.
    body = strip_csrf(html)
    assert body.count(ANN_NAME) == (2 if viewer == ANN else 1)
    assert body.count(OWNER_NAME) == (2 if viewer == OWNER else 1)
    assert (f"Signed in as {ANN_NAME}" in body) == (viewer == ANN)
    assert (f"Signed in as {OWNER_NAME}" in body) == (viewer == OWNER)
