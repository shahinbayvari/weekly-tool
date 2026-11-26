"""The shared base template: header, role-aware nav, escaping, stylesheet and error pages (#22)."""

import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.invites import hash_token
from app.main import app
from app.models import Invite
from app.pages.submit import get_now, get_settings
from app.users import create_user
from tests.conftest import CsrfClient
from tests.test_security import inline_problems

PW = "correct horse"
AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=AMS)
ROOT = Path(__file__).resolve().parent.parent
MEMBER_LINKS = [("Submit", "/submit"), ("Reveal", "/reveal")]
OWNER_LINKS = MEMBER_LINKS + [("Invites", "/invites"), ("Missing", "/missing")]
PAGES = ["/submit", "/reveal", "/invites", "/missing"]
HEADER = re.compile(r"<header.*?</header>", re.S)


@pytest.fixture(autouse=True)
def clock():
    app.dependency_overrides[get_now] = lambda: NOW
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield
    app.dependency_overrides.pop(get_now, None)
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def users(db_engine):
    return {
        "owner": create_user("olga@example.com", PW, "owner", display_name="Olga Owner", engine=db_engine),
        "member": create_user("mia@example.com", PW, "member", display_name="Mia Member", engine=db_engine),
    }


def set_project(engine, name):
    with engine.begin() as c:
        c.execute(text("DELETE FROM project"))
        c.execute(text("INSERT INTO project (id, name) VALUES (1, :n)"), {"n": name})


def signed_in(email):
    c = CsrfClient(app, follow_redirects=False)
    assert c.post("/login", data={"email": email, "password": PW}).status_code == 303
    return c


class Page(HTMLParser):
    """Collects the links (label, href, aria-current) inside <nav>, and landmark facts."""

    def __init__(self, html):
        super().__init__()
        self.nav_links, self.tags = [], []
        self.nav_label = self.html_lang = None
        self.viewport = False
        self.stylesheets = []
        self._in_nav = False
        self._href = None
        self._current = None
        self._label = ""
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append(tag)
        if tag == "html":
            self.html_lang = a.get("lang")
        if tag == "meta" and a.get("name") == "viewport":
            self.viewport = True
        if tag == "link" and a.get("rel") == "stylesheet":
            self.stylesheets.append(a.get("href"))
        if tag == "nav":
            self._in_nav = True
            self.nav_label = a.get("aria-label")
        if tag == "a":
            self._href, self._current, self._label = a.get("href"), a.get("aria-current"), ""

    def handle_data(self, data):
        if self._href is not None:
            self._label += data

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            if self._in_nav:
                self.nav_links.append((self._label.strip(), self._href, self._current))
            self._href = None
        if tag == "nav":
            self._in_nav = False


def links(html):
    return [(label, href) for label, href, _ in Page(html).nav_links]


def current(html):
    return [href for _, href, cur in Page(html).nav_links if cur == "page"]


# --- links per role --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/submit", "/reveal"])
def test_member_sees_submit_and_reveal_only(db_engine, users, path):
    with signed_in("mia@example.com") as c:
        html = c.get(path).text
    assert links(html) == MEMBER_LINKS
    assert "/invites" not in html and "/missing" not in html


@pytest.mark.parametrize("path", PAGES)
def test_owner_sees_all_four_links_on_every_page(db_engine, users, path):
    with signed_in("olga@example.com") as c:
        r = c.get(path)
    assert r.status_code == 200
    assert links(r.text) == OWNER_LINKS


@pytest.mark.parametrize("path", PAGES)
def test_aria_current_marks_only_the_current_page(db_engine, users, path):
    with signed_in("olga@example.com") as c:
        page = Page(c.get(path).text)
    assert [href for _, href, cur in page.nav_links if cur == "page"] == [path]
    assert all(cur in (None, "page") for _, _, cur in page.nav_links)


@pytest.mark.parametrize("path", ["/submit", "/reveal"])
def test_aria_current_for_member_pages(db_engine, users, path):
    with signed_in("mia@example.com") as c:
        assert current(c.get(path).text) == [path]


def test_invites_error_rerender_keeps_current_marker(db_engine, users):
    with signed_in("olga@example.com") as c:
        r = c.post("/invites", data={"email": "bad"})
    assert r.status_code == 400 and current(r.text) == ["/invites"]


def test_landmarks_language_viewport_and_one_stylesheet(db_engine, users):
    with signed_in("mia@example.com") as c:
        page = Page(c.get("/submit").text)
    assert page.nav_label == "Main"
    assert {"header", "nav", "main"} <= set(page.tags)
    assert page.html_lang == "en" and page.viewport
    assert page.stylesheets == ["/static/style.css"]


# --- header ----------------------------------------------------------------------


def test_header_shows_project_own_name_and_logout_form_with_token(db_engine, users):
    set_project(db_engine, "Team Rocket")
    with signed_in("mia@example.com") as c:
        header = HEADER.search(c.get("/submit").text).group(0)
        assert "Team Rocket" in header and "Signed in as Mia Member" in header
        assert "mia@example.com" not in header and "@" not in header
        assert "Olga Owner" not in header
        form = re.search(r'<form method="post" action="/logout">.*?</form>', header, re.S).group(0)
        assert re.search(r'name="csrf_token" value="([^"]+)"', form).group(1) == c.csrf_token()
        assert "Log out" in form and "<button" in form
        assert c.post("/logout").status_code == 303  # the form works with its token


def test_logout_form_present_once_on_every_signed_in_page(db_engine, users):
    with signed_in("olga@example.com") as c:
        for path in PAGES:
            html = c.get(path).text
            assert html.count('action="/logout"') == 1 and "Log out" in html


def test_default_project_name_when_none_exists(db_engine, users):
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM project"))
    with signed_in("mia@example.com") as c:
        assert "Weekly Feedback" in HEADER.search(c.get("/submit").text).group(0)


def test_project_name_and_display_name_are_escaped(db_engine):
    set_project(db_engine, "<b>x</b>")
    email = "e@example.com"
    create_user(email, PW, "owner", display_name="e<i>y</i>", engine=db_engine)
    with CsrfClient(app, follow_redirects=False) as c:
        c.post("/login", data={"email": email, "password": PW})
        html = c.get("/submit").text
    header = HEADER.search(html).group(0)
    assert "&lt;b&gt;x&lt;/b&gt;" in header and "<b>x</b>" not in html
    assert "<i>y</i>" not in html and "&lt;i&gt;y&lt;/i&gt;" in header
    assert "&lt;b&gt;x&lt;/b&gt;" in re.search(r"<title>.*?</title>", html).group(0)


def test_reveal_header_has_nothing_about_the_current_week(db_engine, users):
    with signed_in("olga@example.com") as c:
        header = HEADER.search(c.get("/reveal").text).group(0)
    assert not re.search(r"\d{4}-\d{2}-\d{2}|Week of|Nov|submitted", header)
    assert header.count("@") == 0  # the header shows the viewer's name, never an email
    assert "Signed in as Olga Owner" in header


# --- signed-out pages ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/invite/not-a-real-token"])
def test_signed_out_pages_show_only_project_name(db_engine, users, path):
    set_project(db_engine, "Team Rocket")
    with CsrfClient(app, follow_redirects=False) as c:
        html = c.get(path).text
    header = HEADER.search(html).group(0)
    assert "Team Rocket" in header and "<a " not in header
    assert "<nav" not in html and "/logout" not in html and "Log out" not in html


def test_setup_page_uses_base_with_default_name(db_engine):
    with CsrfClient(app, follow_redirects=False) as c:
        html = c.get("/setup").text
    assert "Weekly Feedback" in HEADER.search(html).group(0)
    assert "<nav" not in html and "/logout" not in html


def test_accept_invite_page_is_signed_out_layout(db_engine, users):
    with Session(db_engine) as s:
        s.add(Invite(email="new@example.com", token_hash=hash_token("tok-1"), invited_by=users["owner"].id))
        s.commit()
    with CsrfClient(app, follow_redirects=False) as c:
        html = c.get("/invite/tok-1").text
    assert "Join the team" in html and "<nav" not in html and "/logout" not in html
    assert "Weekly Feedback" in HEADER.search(html).group(0)


# --- stylesheet, inline styles/scripts ----------------------------------------------


def test_one_stylesheet_and_no_app_css():
    assert (ROOT / "app/static/style.css").is_file()
    assert not (ROOT / "app/static/app.css").exists()
    for f in (ROOT / "app/templates").glob("*.html"):
        assert "app.css" not in f.read_text(encoding="utf-8")
    assert "inline-form" in (ROOT / "app/static/style.css").read_text(encoding="utf-8")


def test_stylesheet_served_and_pages_link_it(db_engine, users):
    with signed_in("olga@example.com") as c:
        assert c.get("/static/style.css").status_code == 200
        assert c.get("/static/app.css").status_code == 404
        for path in PAGES:
            html = c.get(path).text
            assert html.count('rel="stylesheet"') == 1 and '<link rel="stylesheet" href="/static/style.css">' in html
            assert inline_problems(html) == []


def test_every_template_extends_the_base():
    for f in (ROOT / "app/templates").glob("*.html"):
        if f.name == "base.html" or f.name.startswith("_"):
            continue
        assert f.read_text(encoding="utf-8").startswith('{% extends "base.html" %}'), f.name


def test_stylesheet_has_no_external_urls_and_keeps_focus_outline():
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    assert "http://" not in css and "https://" not in css and "@import" not in css
    assert ":focus-visible" in css and "outline: none" not in css and "outline:none" not in css
    assert css.count("min-height: 44px") >= 3


def _luminance(hex_colour):
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_and_link_colours_meet_contrast_4_5():
    css = (ROOT / "app/static/style.css").read_text(encoding="utf-8")
    var = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", css))
    pairs = [
        ("text", "bg"), ("text", "header-bg"), ("muted", "bg"), ("muted", "header-bg"),
        ("link", "bg"), ("link", "header-bg"), ("button-text", "button"), ("button-text", "button-hover"),
        ("error", "bg"),
    ]
    for fg, bg in pairs:
        assert contrast(var[fg], var[bg]) >= 4.5, (fg, bg, contrast(var[fg], var[bg]))
    # the focus ring is visible against both backgrounds and differs from the link colour
    assert contrast(var["focus"], var["bg"]) >= 3 and contrast(var["focus"], var["header-bg"]) >= 3
    assert var["focus"] != var["link"]


# --- error pages -----------------------------------------------------------------------

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def test_404_html_for_browsers_with_layout_and_nav(db_engine, users):
    with signed_in("olga@example.com") as c:
        r = c.get("/no-such-page", headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Page not found" in r.text and '<link rel="stylesheet" href="/static/style.css">' in r.text
    assert links(r.text) == OWNER_LINKS and current(r.text) == []


def test_404_signed_out_html_has_no_links(db_engine):
    with CsrfClient(app, follow_redirects=False) as c:
        r = c.get("/no-such-page", headers=HTML)
    assert r.status_code == 404 and "Page not found" in r.text and "<nav" not in r.text


def test_404_stays_json_for_non_html_clients(db_engine):
    with CsrfClient(app, follow_redirects=False) as c:
        for headers in ({}, {"Accept": "*/*"}, {"Accept": "application/json"}):
            r = c.get("/no-such-page", headers=headers)
            assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_403_html_for_browsers_plain_text_otherwise(db_engine, users):
    with signed_in("mia@example.com") as c:
        r = c.get("/invites", headers=HTML)
        assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
        assert "Not allowed" in r.text and links(r.text) == MEMBER_LINKS
        plain = c.get("/invites")
    assert plain.status_code == 403 and plain.text == "Not allowed"
    assert plain.headers["content-type"].startswith("text/plain")


def test_redirects_still_redirect_for_browsers(db_engine, users):
    with CsrfClient(app, follow_redirects=False) as c:
        r = c.get("/submit", headers=HTML)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fsubmit"


def test_error_pages_have_no_inline_styles_or_scripts(db_engine, users):
    with signed_in("mia@example.com") as c:
        for path in ("/invites", "/nope"):
            assert inline_problems(c.get(path, headers=HTML).text) == []


def test_header_shows_each_viewers_own_name_and_never_an_email(db_engine, users):
    for email, mine, other in (
        ("olga@example.com", "Olga Owner", "Mia Member"),
        ("mia@example.com", "Mia Member", "Olga Owner"),
    ):
        with signed_in(email) as c:
            for path in PAGES[:2] if email.startswith("mia") else PAGES:
                header = HEADER.search(c.get(path).text).group(0)
                assert f"Signed in as {mine}" in header
                assert other not in header and "@" not in header
                assert "olga" not in header.lower().replace("olga owner", "")
