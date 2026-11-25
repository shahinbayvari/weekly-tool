import base64
import json
import re

import pytest
from fastapi import Depends
from tests.conftest import CsrfClient
from sqlalchemy import text

from app.auth import current_user, safe_next
from app.main import app
from app.users import create_user

PW = "correct horse"


@pytest.fixture
def client(db_engine):
    # db_engine points WEEKLY_DB_PATH at the isolated, migrated test database
    with CsrfClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def ann(db_engine):
    return create_user("ann@example.com", PW, "member", engine=db_engine)


def login(client, email="ann@example.com", password=PW, **extra):
    return client.post("/login", data={"email": email, "password": password, **extra})


def test_login_form(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'name="email"' in r.text and 'name="password"' in r.text and 'type="submit"' in r.text
    assert "Invalid email or password" not in r.text


def test_login_success_redirects_and_signs_in(client, ann):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    home = client.get("/submit")
    assert home.status_code == 200 and "ann@example.com" in home.text


def test_email_trimmed_and_lowercased(client, ann):
    r = login(client, email="  Ann@Example.com")
    assert r.status_code == 303
    assert "ann@example.com" in client.get("/submit").text


def test_wrong_password_and_unknown_email_identical(client, ann):
    wrong = login(client, password="wrong password")
    unknown = login(client, email="nobody@example.com")
    for r in (wrong, unknown):
        assert r.status_code == 401
        assert "Invalid email or password" in r.text
    strip = lambda html: re.sub(r'name="email" value="[^"]*"', "", html)  # noqa: E731
    assert strip(wrong.text).replace("ann@example.com", "") == strip(unknown.text)
    assert client.get("/").status_code == 303  # still signed out


@pytest.mark.parametrize("email,password", [("", PW), ("ann@example.com", ""), ("", ""), ("   ", PW)])
def test_empty_fields_same_as_wrong(client, ann, email, password):
    r = login(client, email=email, password=password)
    assert r.status_code == 401
    assert "Invalid email or password" in r.text


def test_missing_fields_same_as_wrong(client, ann):
    r = client.post("/login", data={})
    assert r.status_code == 401 and "Invalid email or password" in r.text


def test_failed_login_keeps_email_not_password(client, ann):
    r = login(client, email="ann@example.com", password="s3cret-wrong-pw")
    assert 'value="ann@example.com"' in r.text
    assert "s3cret-wrong-pw" not in r.text


def test_typed_email_is_escaped(client):
    r = login(client, email='"><script>x</script>')
    assert "<script>x</script>" not in r.text


def test_next_is_followed(client, ann):
    form = client.get("/login?next=/submit")
    assert 'name="next" value="/submit"' in form.text
    r = login(client, next="/submit")
    assert r.status_code == 303 and r.headers["location"] == "/submit"


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example",
        "//evil.example",
        "/\\evil.example",
        "\\\\evil.example",
        "/\t/evil.example",
        "evil.example",
        "javascript:alert(1)",
        "",
        "/a\nb",
    ],
)
def test_unsafe_next_ignored(client, ann, bad):
    r = login(client, next=bad)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_unsafe_next_ignored_in_form(client):
    r = client.get("/login", params={"next": "//evil.example"})
    assert 'name="next" value="/"' in r.text


def test_safe_next_unit():
    assert safe_next("/submit") == "/submit"
    assert safe_next("/a?b=1") == "/a?b=1"
    assert safe_next(None) == "/"
    assert safe_next("/\\evil") == "/"


def test_login_redirects_when_signed_in(client, ann):
    login(client)
    r = client.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_cookie_flags_and_contents(client, ann):
    r = login(client)
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    value = client.cookies.get("weekly_session")
    assert value
    assert "ann@example.com" not in value and ann.password_hash not in value
    payload = base64.b64decode(value.split(".")[0])
    assert json.loads(payload) == {"user_id": ann.id}
    assert b"ann@example.com" not in payload and b"argon2" not in payload


def test_tampered_cookie_signs_out(client, ann):
    login(client)
    value = client.cookies.get("weekly_session")
    for i in range(len(value)):
        bad = value[:i] + ("A" if value[i] != "A" else "B") + value[i + 1 :]
        client.cookies.set("weekly_session", bad)
        assert client.get("/").status_code == 303, f"char {i}"


def test_cookie_for_deleted_user_signs_out(client, ann, db_engine):
    create_user("bob@example.com", PW, "member", engine=db_engine)  # keeps setup closed
    login(client)
    assert client.get("/submit").status_code == 200
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM users WHERE id = :i"), {"i": ann.id})
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.get("/login").status_code == 200  # form shown, not redirected


def test_current_user_redirects_with_next(client):
    @app.get("/_protected")
    def protected(user=Depends(current_user)):
        return {"email": user.email}

    try:
        r = client.get("/_protected")
        assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F_protected"
        r = client.get("/_protected?x=1")
        assert r.headers["location"] == "/login?next=%2F_protected%3Fx%3D1"
    finally:
        app.router.routes[:] = [x for x in app.router.routes if getattr(x, "path", "") != "/_protected"]


def test_logout_clears_session(client, ann):
    login(client)
    r = client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/").status_code == 303


def test_logout_get_not_allowed(client, ann):
    login(client)
    assert client.get("/logout").status_code == 405
    assert client.get("/submit").status_code == 200  # still signed in


def test_landing_redirects_to_login_when_signed_out(client, ann):  # a user exists, so not /setup
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F"


def test_landing_redirects_signed_in_user_to_submit(client, ann):
    login(client)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/submit"
