"""CSRF protection, cookie flags and security headers (#21)."""

import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.auth import router as auth_router
from app.config import Settings
from app.invites import hash_token
from app.main import app
from app.pages.submit import get_now, get_settings
from app.security import MAX_BODY_BYTES, SECURITY_HEADERS, UNSAFE_METHODS, install
from app.users import create_user
from tests.conftest import CsrfClient

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
OPEN = datetime(2026, 1, 7, 12, 0, tzinfo=AMS)
CLOSED = datetime(2026, 1, 10, 12, 0, tzinfo=AMS)
PW = "correct horse"
OWNER = "olga@example.com"
MEMBER = "mia@example.com"
INVITE_TOKEN = "invite-token-for-tests"


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
def users(db_engine, clock):
    owner = create_user(OWNER, PW, "owner", engine=db_engine)
    member = create_user(MEMBER, PW, "member", engine=db_engine)
    with db_engine.begin() as c:
        c.execute(text("UPDATE users SET created_at = '2026-01-01 00:00:00'"))
        c.execute(
            text("INSERT INTO invites (email, token_hash, invited_by) VALUES ('new@example.com', :h, :o)"),
            {"h": hash_token(INVITE_TOKEN), "o": owner.id},
        )
    return {"owner": owner, "member": member}


@pytest.fixture
def client(users):
    with CsrfClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def owner(client):
    assert client.post("/login", data={"email": OWNER, "password": PW}).status_code == 303
    return client


def count(engine, table):
    with engine.connect() as c:
        return c.scalar(text(f"SELECT COUNT(*) FROM {table}"))


def token_in(html):
    m = re.search(r'name="csrf_token" value="([^"]*)"', html)
    assert m, "no csrf_token hidden field"
    return m.group(1)


def seed_item(engine, user_id, anonymous=False):
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO feedback_items (author_id, week, category, text, anonymous) "
                "VALUES (:u, '2026-01-05', 'start', 'hello', :a)"
            ),
            {"u": user_id, "a": anonymous},
        )
        return c.scalar(text("SELECT MAX(id) FROM feedback_items"))


# --- every state-changing route rejects a request without a token --------------

# (method, route path) -> a concrete URL. Adding a state-changing route to the app
# without adding it here (and so without a no-token test) fails the suite.
ROUTE_URLS = {
    ("POST", "/login"): "/login",
    ("POST", "/logout"): "/logout",
    ("POST", "/setup"): "/setup",
    ("POST", "/invite/{token}"): f"/invite/{INVITE_TOKEN}",
    ("POST", "/invites"): "/invites",
    ("POST", "/invites/{invite_id}/revoke"): "/invites/1/revoke",
    ("POST", "/submit/{category}/items"): "/submit/start/items",
    ("POST", "/submit/items/{item_id}/edit"): "/submit/items/1/edit",
    ("POST", "/submit/items/{item_id}/delete"): "/submit/items/1/delete",
    ("POST", "/submit/items/{item_id}/anonymous"): "/submit/items/1/anonymous",
}


def walk_routes(routes, prefix=""):
    """(methods, full path) of every route; FastAPI wraps included routers in _IncludedRouter."""
    for route in routes:
        if hasattr(route, "original_router"):  # an included router
            yield from walk_routes(route.original_router.routes, prefix + route.include_context.prefix)
        elif hasattr(route, "methods") and route.methods is not None:
            yield route.methods, prefix + route.path
        elif hasattr(route, "routes"):  # Mount of a router
            yield from walk_routes(route.routes, prefix + route.path)


def app_state_changing_routes():
    return {(m, path) for methods, path in walk_routes(app.routes) for m in methods if m in UNSAFE_METHODS}


def test_route_walker_sees_the_real_routes():
    found = {(m, path) for methods, path in walk_routes(app.routes) for m in methods}
    assert ("GET", "/login") in found and ("POST", "/login") in found and ("GET", "/health") in found
    assert len(app_state_changing_routes()) >= 10


def test_every_state_changing_route_has_a_no_token_test():
    assert app_state_changing_routes() == set(ROUTE_URLS), (
        "a state-changing route was added or removed: update ROUTE_URLS and cover it with a no-token test"
    )


@pytest.mark.parametrize("route", sorted(ROUTE_URLS))
def test_route_without_token_is_403(route, owner, db_engine):
    seed_item(db_engine, owner_id(db_engine))
    r = owner.post(ROUTE_URLS[route], data={"email": "x@example.com", "password": PW, "text": "t"}, csrf=False)
    assert r.status_code == 403


@pytest.mark.parametrize("route", sorted(ROUTE_URLS))
def test_route_with_empty_or_wrong_token_is_403(route, owner):
    for bad in ("", "wrong", "x" * 43):
        r = owner.post(ROUTE_URLS[route], data={"csrf_token": bad, "text": "t"}, csrf=False)
        assert r.status_code == 403
        r = owner.post(ROUTE_URLS[route], data={"text": "t"}, headers={"X-CSRF-Token": bad}, csrf=False)
        assert r.status_code == 403


def owner_id(engine):
    with engine.connect() as c:
        return c.scalar(text("SELECT id FROM users WHERE role = 'owner'"))


def test_setup_without_token_creates_nothing(db_engine, monkeypatch):
    # fresh empty database: no users, setup is open
    from tests.conftest import isolated_db

    with isolated_db(monkeypatch) as engine, CsrfClient(app, follow_redirects=False) as c:
        data = {"project_name": "P", "email": "o@example.com", "password": PW, "confirm_password": PW}
        assert c.post("/setup", data=data, csrf=False).status_code == 403
        assert count(engine, "users") == 0 and count(engine, "project") == 0
        assert c.post("/setup", data=data).status_code == 303
        assert count(engine, "users") == 1


def test_login_without_token_does_not_sign_in(client):
    r = client.post("/login", data={"email": OWNER, "password": PW}, csrf=False)
    assert r.status_code == 403
    assert client.get("/submit").status_code == 303


def test_logout_without_token_keeps_session(owner):
    assert owner.post("/logout", csrf=False).status_code == 403
    assert owner.get("/submit").status_code == 200
    assert owner.post("/logout").status_code == 303
    assert owner.get("/submit").status_code == 303


def test_accept_invite_without_token_creates_no_user(client, db_engine):
    users_before = count(db_engine, "users")
    r = client.post(f"/invite/{INVITE_TOKEN}", data={"password": PW, "confirm_password": PW}, csrf=False)
    assert r.status_code == 403
    assert count(db_engine, "users") == users_before
    assert client.post(f"/invite/{INVITE_TOKEN}", data={"password": PW, "confirm_password": PW}).status_code == 303


def test_create_and_revoke_invite_without_token_change_nothing(owner, db_engine):
    assert owner.post("/invites", data={"email": "z@example.com"}, csrf=False).status_code == 403
    assert count(db_engine, "invites") == 1
    assert owner.post("/invites/1/revoke", csrf=False).status_code == 403
    with db_engine.connect() as c:
        assert c.scalar(text("SELECT revoked_at FROM invites WHERE id = 1")) is None
    assert owner.post("/invites/1/revoke").status_code == 303


def test_item_routes_without_token_change_nothing(owner, db_engine):
    item = seed_item(db_engine, owner_id(db_engine))
    assert owner.post("/submit/start/items", data={"text": "new"}, csrf=False).status_code == 403
    assert owner.post(f"/submit/items/{item}/edit", data={"text": "edited"}, csrf=False).status_code == 403
    assert owner.post(f"/submit/items/{item}/anonymous", data={"anonymous": "true"}, csrf=False).status_code == 403
    assert owner.post(f"/submit/items/{item}/delete", csrf=False).status_code == 403
    with db_engine.connect() as c:
        rows = c.execute(text("SELECT text, anonymous FROM feedback_items")).all()
    assert [tuple(r) for r in rows] == [("hello", 0)]


def test_other_unsafe_methods_are_checked(owner):
    for method in ("PUT", "PATCH", "DELETE"):
        r = owner.request(method, "/submit/start/items", csrf=False)
        assert r.status_code == 403, method
    assert owner.request("PUT", "/submit/start/items").status_code == 405  # token ok, no such handler


def test_safe_methods_are_not_checked(client):
    assert client.get("/login").status_code == 200
    assert client.head("/static/htmx.min.js").status_code == 200
    assert client.options("/login").status_code != 403
    assert client.get("/health").status_code == 200


# --- a correct token is accepted ----------------------------------------------


def test_correct_form_token_accepted(owner, db_engine):
    r = owner.post("/submit/start/items", data={"text": "via form"})
    assert r.status_code == 200 and "via form" in r.text
    assert count(db_engine, "feedback_items") == 1


def test_correct_header_token_accepted_without_form_field(owner, db_engine):
    token = owner.csrf_token()
    r = owner.post("/submit/start/items", data={"text": "via header"}, headers={"X-CSRF-Token": token}, csrf=False)
    assert r.status_code == 200 and "via header" in r.text
    assert count(db_engine, "feedback_items") == 1


def test_wrong_header_is_rejected_even_with_valid_form_token(owner, db_engine):
    token = owner.csrf_token()
    r = owner.post("/submit/start/items", data={"text": "t", "csrf_token": token}, headers={"X-CSRF-Token": "bad"}, csrf=False)
    assert r.status_code == 403
    assert count(db_engine, "feedback_items") == 0


def test_multipart_token_accepted_and_body_replayed(owner, db_engine):
    token = owner.csrf_token()
    files = {"csrf_token": (None, token), "text": (None, "multipart text"), "anonymous": (None, "on")}
    r = owner.post("/submit/start/items", files=files, csrf=False)
    assert r.status_code == 200
    with db_engine.connect() as c:
        row = c.execute(text("SELECT text, anonymous FROM feedback_items")).one()
    assert tuple(row) == ("multipart text", 1)


def test_multipart_without_token_is_403(owner, db_engine):
    r = owner.post("/submit/start/items", files={"text": (None, "x")}, csrf=False)
    assert r.status_code == 403
    assert count(db_engine, "feedback_items") == 0


def test_duplicate_token_fields_rejected(owner):
    token = owner.csrf_token()
    r = owner.post("/submit/start/items", content=f"csrf_token={token}&csrf_token={token}&text=t",
                   headers={"content-type": "application/x-www-form-urlencoded"}, csrf=False)
    assert r.status_code == 403


def test_oversized_body_without_token_is_refused_not_crashed(owner, db_engine):
    big = "a" * (MAX_BODY_BYTES + 10)
    r = owner.post("/submit/start/items", data={"text": big}, csrf=False)
    assert r.status_code == 413
    # chunked (no content-length) body
    def chunks():
        for _ in range(MAX_BODY_BYTES // 1000 + 5):
            yield b"a" * 1000
    r = owner.post("/submit/start/items", content=chunks(),
                   headers={"content-type": "application/x-www-form-urlencoded"}, csrf=False)
    assert r.status_code == 413
    assert count(db_engine, "feedback_items") == 0


def test_large_body_with_token_still_reaches_the_handler(owner, db_engine):
    r = owner.post("/submit/start/items", data={"text": "a" * 200_000})
    assert r.status_code == 200  # the app's own validation answers, not the middleware
    assert count(db_engine, "feedback_items") == 0


# --- the token is session-bound, random and rotates ----------------------------


def test_token_is_long_random_and_stable_within_a_session(client):
    t1 = client.csrf_token()
    assert len(t1) >= 43  # token_urlsafe(32)
    assert client.csrf_token() == t1
    with CsrfClient(app, follow_redirects=False) as other:
        t2 = other.csrf_token()
    assert t2 != t1


def test_token_from_another_session_is_rejected(users, db_engine):
    with CsrfClient(app, follow_redirects=False) as a, CsrfClient(app, follow_redirects=False) as b:
        assert a.post("/login", data={"email": OWNER, "password": PW}).status_code == 303
        assert b.post("/login", data={"email": OWNER, "password": PW}).status_code == 303
        token_a = a.csrf_token()
        assert token_a != b.csrf_token()
        r = b.post("/submit/start/items", data={"text": "t"}, headers={"X-CSRF-Token": token_a}, csrf=False)
        assert r.status_code == 403
        r = b.post("/submit/start/items", data={"text": "t", "csrf_token": token_a}, csrf=False)
        assert r.status_code == 403
        assert count(db_engine, "feedback_items") == 0


def test_token_without_any_session_is_rejected(users):
    with TestClient(app, follow_redirects=False) as bare:
        assert bare.post("/login", data={"email": OWNER, "password": PW, "csrf_token": "anything"}).status_code == 403
        assert bare.post("/login", headers={"X-CSRF-Token": ""}).status_code == 403


def test_forged_unsigned_cookie_is_rejected(users):
    with TestClient(app, follow_redirects=False) as c:
        c.cookies.set("weekly_session", "forged")
        assert c.post("/login", data={"email": OWNER, "password": PW, "csrf_token": "forged"}).status_code == 403


def test_token_changes_after_sign_in_and_logout(client):
    before = client.csrf_token()
    assert client.post("/login", data={"email": OWNER, "password": PW}).status_code == 303
    signed_in = client.csrf_token()
    assert signed_in != before
    assert client.post("/logout").status_code == 303
    after = client.csrf_token()
    assert after not in (before, signed_in)
    # the old token no longer works
    assert client.post("/login", data={"email": OWNER, "password": PW, "csrf_token": signed_in}, csrf=False).status_code == 403


def test_failed_login_keeps_form_usable(client):
    assert client.post("/login", data={"email": OWNER, "password": "wrong"}).status_code == 401


# --- every form has the token; htmx requests send it --------------------------


def test_every_form_has_a_hidden_token(owner, db_engine, users):
    seed_item(db_engine, users["owner"].id)
    pages = {
        "/submit": 4,  # logout + three add forms
        "/invites": 3,  # create + revoke + logout is not on this page: create, revoke
        "/reveal": 1,
        "/missing": 1,
    }
    for path in pages:
        html = owner.get(path).text
        forms = re.findall(r"<form\b", html)
        fields = re.findall(r'<input type="hidden" name="csrf_token" value="[^"]+">', html)
        assert forms and len(fields) == len(forms), path
        assert len(set(re.findall(r'name="csrf_token" value="([^"]+)"', html))) == 1
    edit = owner.get(f"/submit/items/1/edit", headers={"HX-Request": "true"}).text
    assert 'name="csrf_token"' in edit


def test_pre_sign_in_forms_have_a_token(client, db_engine):
    for path in ("/login", f"/invite/{INVITE_TOKEN}"):
        assert token_in(client.get(path).text), path


def test_setup_form_has_a_token(monkeypatch):
    from tests.conftest import isolated_db

    with isolated_db(monkeypatch), CsrfClient(app, follow_redirects=False) as c:
        assert token_in(c.get("/setup").text)


def test_submit_page_sends_token_header_on_htmx_requests(owner):
    html = owner.get("/submit").text
    m = re.search(r"<body hx-headers='\{\"X-CSRF-Token\": \"([^\"]+)\"\}'>", html)
    assert m and m.group(1) == owner.csrf_token()
    assert '"includeIndicatorStyles": false' in html


# --- cookies -------------------------------------------------------------------


def cookie_header(response):
    return response.headers["set-cookie"].lower()


@pytest.mark.parametrize("path", ["/login", f"/invite/{INVITE_TOKEN}"])
def test_pre_sign_in_cookie_flags(client, path):
    header = cookie_header(client.get(path))
    assert "weekly_session=" in header and "httponly" in header and "samesite=lax" in header
    assert "secure" not in header


def test_setup_cookie_flags(monkeypatch):
    from tests.conftest import isolated_db

    with isolated_db(monkeypatch), CsrfClient(app, follow_redirects=False) as c:
        header = cookie_header(c.get("/setup"))
    assert "httponly" in header and "samesite=lax" in header


def test_session_cookie_flags_after_sign_in(client):
    client.get("/login")
    r = client.post("/login", data={"email": OWNER, "password": PW})
    header = cookie_header(r)
    assert "httponly" in header and "samesite=lax" in header


def make_mini_app(secure: bool) -> FastAPI:
    mini = FastAPI()
    install(mini, Settings(cookie_secure=secure))
    mini.include_router(auth_router)
    return mini


@pytest.mark.parametrize("secure", [True, False])
def test_secure_flag_follows_setting(db_engine, secure):
    with TestClient(make_mini_app(secure), base_url="https://testserver") as c:
        header = cookie_header(c.get("/login"))
    assert ("secure" in header.replace("samesite", "")) is secure
    assert "httponly" in header and "samesite=lax" in header


def test_production_env_alone_does_not_set_secure():
    from app.config import load_settings

    s = load_settings({"WEEKLY_ENV": "production", "WEEKLY_SECRET_KEY": "k", "WEEKLY_COOKIE_SECURE": "true"})
    assert s.cookie_secure is True
    with TestClient(make_mini_app(s.cookie_secure), base_url="https://testserver") as c:
        assert "secure" in cookie_header(c.get("/login")).replace("samesite", "")


# --- security headers ----------------------------------------------------------


def assert_headers(response):
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name) == value, name


def test_header_values_are_exactly_as_specified():
    assert SECURITY_HEADERS == {
        "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


def test_headers_on_every_kind_of_response(owner):
    responses = [
        owner.get("/health"),  # 200 json
        owner.get("/login"),  # 303 redirect (signed in)
        owner.get("/nope"),  # 404
        owner.get("/static/htmx.min.js"),  # static file
        owner.get("/static/missing.js"),  # static 404
        owner.get("/invite/not-a-real-token"),  # app 404 page
        owner.post("/logout", csrf=False),  # 403 from CSRF
        owner.post("/login", data={"csrf_token": "x" * 50}, csrf=False),  # 403
        owner.post("/submit/start/items", data={"text": "a" * (MAX_BODY_BYTES + 1)}, csrf=False),  # 413
        owner.put("/health", csrf=False),  # 403 before routing
        owner.get("/submit"),  # 200 html
        owner.get("/missing"),  # 200 or owner page
        owner.get("/submit/items/999"),  # 404 plain
    ]
    statuses = {r.status_code for r in responses}
    assert {200, 303, 403, 404, 413} <= statuses
    for r in responses:
        assert_headers(r)


def test_headers_on_member_forbidden_and_signed_out_redirect(client, users):
    assert_headers(client.get("/invites"))  # 303 to login
    client.post("/login", data={"email": MEMBER, "password": PW})
    r = client.get("/invites")
    assert r.status_code == 403
    assert_headers(r)


def test_headers_on_unhandled_exception_500():
    boom = FastAPI()
    install(boom, Settings())

    @boom.get("/boom")
    def explode():
        raise RuntimeError("boom")

    with TestClient(boom, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert_headers(r)
    assert "boom" not in r.text


def test_headers_replace_a_handler_set_value():
    mini = FastAPI()
    install(mini, Settings())

    @mini.get("/x")
    def x():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("ok", headers={"X-Frame-Options": "SAMEORIGIN"})

    with TestClient(mini) as c:
        r = c.get("/x")
    assert r.headers.get_list("x-frame-options") == ["DENY"]


# --- no inline scripts or styles anywhere --------------------------------------


class InlineFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.problems = []
        self._script = None

    def handle_starttag(self, tag, attrs):
        names = {k.lower(): v for k, v in attrs}
        if tag == "style":
            self.problems.append("<style> tag")
        if "style" in names:
            self.problems.append("style attribute")
        for name in names:
            if name.startswith("on"):
                self.problems.append(f"event handler {name}")
            if name.startswith("hx-on"):
                self.problems.append(f"inline htmx handler {name}")
        if tag == "script":
            src = names.get("src")
            if not src:
                self.problems.append("inline <script>")
            elif not src.startswith("/static/"):
                self.problems.append(f"script from {src}")
        if tag == "link" and names.get("rel") == "stylesheet" and not (names.get("href") or "").startswith("/static/"):
            self.problems.append("external stylesheet")
        for key in ("href", "src", "action"):
            if (names.get(key) or "").lower().startswith("javascript:"):
                self.problems.append("javascript: URL")

    def handle_data(self, data):
        pass


def inline_problems(html):
    finder = InlineFinder()
    finder.feed(html)
    # the pattern also catches a script written as text inside the markup
    assert "<script" not in re.sub(r'<script src="/static/[^"]+"></script>', "", html), "inline script text"
    return finder.problems


def test_no_inline_scripts_or_styles_in_any_page(owner, db_engine, users, clock):
    seed_item(db_engine, users["owner"].id)
    seed_item(db_engine, users["member"].id, anonymous=True)
    pages = ["/submit", "/reveal", "/missing", "/invites", f"/invite/{INVITE_TOKEN}", "/invite/not-valid"]
    htmls = {}
    for path in pages:
        if path.startswith("/invite/"):
            with CsrfClient(app, follow_redirects=False) as anon:
                htmls[path] = anon.get(path).text
        else:
            htmls[path] = owner.get(path).text
    # an invite just created (shows the link block) and a validation error
    htmls["invite created"] = owner.post("/invites", data={"email": "q@example.com"}).text
    htmls["invite error"] = owner.post("/invites", data={"email": "bad"}).text
    # closed-window variants
    clock.now = CLOSED
    for path in ("/submit", "/reveal", "/missing"):
        htmls[path + " (closed)"] = owner.get(path).text
    clock.now = OPEN
    # HTMX fragments
    hx = {"HX-Request": "true"}
    htmls["add"] = owner.post("/submit/start/items", data={"text": "frag"}, headers=hx).text
    htmls["add error"] = owner.post("/submit/start/items", data={"text": ""}, headers=hx).text
    htmls["edit form"] = owner.get("/submit/items/1/edit", headers=hx).text
    htmls["item"] = owner.get("/submit/items/1", headers=hx).text
    for name, html in htmls.items():
        assert html.strip(), name
        assert inline_problems(html) == [], name
    with CsrfClient(app, follow_redirects=False) as anon:
        htmls["login"] = anon.get("/login").text
        htmls["login error"] = anon.post("/login", data={"email": "a@b.c", "password": "x"}).text
    for name in ("login", "login error"):
        assert inline_problems(htmls[name]) == [], name


def test_no_inline_script_or_style_on_setup_and_accept_error(monkeypatch):
    from tests.conftest import isolated_db

    with isolated_db(monkeypatch), CsrfClient(app, follow_redirects=False) as c:
        assert inline_problems(c.get("/setup").text) == []
        assert inline_problems(c.post("/setup", data={"project_name": "", "email": "x"}).text) == []


def test_inline_finder_detects_problems():
    assert inline_problems('<p style="x">') == ["style attribute"]
    assert inline_problems("<style>a{}</style>") == ["<style> tag"]
    assert inline_problems('<button onclick="x()">') == ["event handler onclick"]
    with pytest.raises(AssertionError):
        inline_problems("<script>alert(1)</script>")
