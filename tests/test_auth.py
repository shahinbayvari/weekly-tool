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


# --- rehash on login (#27) ----------------------------------------------------------
import logging  # noqa: E402

import argon2  # noqa: E402
from argon2 import PasswordHasher, Type  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

import app.auth as auth_module  # noqa: E402
import app.passwords as passwords_module  # noqa: E402
from app.passwords import needs_rehash, verify_password  # noqa: E402

OLD = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)


def stored(engine, email="ann@example.com"):
    with engine.connect() as c:
        return c.execute(text("SELECT password_hash FROM users WHERE email = :e"), {"e": email}).scalar_one()


def set_hash(engine, value, email="ann@example.com"):
    with engine.begin() as c:
        c.execute(text("UPDATE users SET password_hash = :h WHERE email = :e"), {"h": value, "e": email})


def current_params():
    return argon2.extract_parameters(passwords_module.hash_password("password123"))


def no_leak(engine, caplog, password, *hashes):
    with engine.connect() as c:
        row = " ".join(str(v) for r in c.execute(text("SELECT * FROM users")) for v in r)
    assert password not in row and password not in caplog.text
    for h in hashes:
        assert h not in caplog.text


@pytest.mark.parametrize("hasher", [OLD, PasswordHasher(type=Type.I)], ids=["old-params", "argon2i"])
def test_login_rehashes_outdated_hash(client, ann, db_engine, caplog, hasher):
    old = hasher.hash("password123")
    set_hash(db_engine, old)
    r = login(client, password="password123", next="/submit")
    assert r.status_code == 303 and r.headers["location"] == "/submit"
    assert client.get("/submit").status_code == 200
    new = stored(db_engine)
    assert new != old and needs_rehash(new) is False
    assert argon2.extract_parameters(new) == current_params()
    assert argon2.extract_parameters(new).type == Type.ID
    assert verify_password("password123", new) is True
    no_leak(db_engine, caplog, "password123", old, new)


def test_current_hash_is_left_alone(client, ann, db_engine):
    before = stored(db_engine)
    assert login(client).status_code == 303
    assert stored(db_engine) == before


def test_failed_logins_leave_hash_alone(client, ann, db_engine):
    old = OLD.hash("password123")
    set_hash(db_engine, old)
    assert login(client, password="wrong password").status_code == 401
    assert login(client, email="nobody@example.com", password="password123").status_code == 401
    assert login(client, password="").status_code == 401
    assert stored(db_engine) == old


def test_malformed_stored_hash_is_401_and_unchanged(client, ann, db_engine):
    set_hash(db_engine, "garbage")
    assert login(client, password="password123").status_code == 401
    assert stored(db_engine) == "garbage"


def test_short_old_password_logs_in_and_is_rehashed(client, ann, db_engine):
    old = OLD.hash("short")
    set_hash(db_engine, old)
    assert login(client, password="short").status_code == 303
    new = stored(db_engine)
    assert new != old and needs_rehash(new) is False and verify_password("short", new)


def test_database_failure_during_rehash_does_not_break_login(client, ann, db_engine, caplog, monkeypatch):
    old = OLD.hash("password123")
    set_hash(db_engine, old)

    def boom(*a, **k):
        raise SQLAlchemyError("db down")

    monkeypatch.setattr(auth_module, "update_password_hash_if_unchanged", boom)
    with caplog.at_level(logging.DEBUG):
        r = login(client, password="password123")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/submit").status_code == 200
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
    assert stored(db_engine) == old
    no_leak(db_engine, caplog, "password123", old)


def test_hashing_failure_during_rehash_does_not_break_login(client, ann, db_engine, caplog, monkeypatch):
    old = OLD.hash("password123")
    set_hash(db_engine, old)

    def boom(*a, **k):
        raise argon2.exceptions.HashingError("no memory")

    monkeypatch.setattr(auth_module, "hash_existing_password", boom)
    with caplog.at_level(logging.DEBUG):
        r = login(client, password="password123")
    assert r.status_code == 303 and client.get("/submit").status_code == 200
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)
    assert stored(db_engine) == old
    no_leak(db_engine, caplog, "password123", old)


def test_rehash_is_compare_and_set(client, ann, db_engine, caplog, monkeypatch):
    old = OLD.hash("password123")
    other = PasswordHasher(time_cost=2, memory_cost=8, parallelism=1).hash("password123")
    set_hash(db_engine, old)
    real = auth_module.hash_existing_password

    def hash_then_race(password):
        new = real(password)
        set_hash(db_engine, other)  # another request replaces the hash before our write
        return new

    monkeypatch.setattr(auth_module, "hash_existing_password", hash_then_race)
    r = login(client, password="password123")
    assert r.status_code == 303 and client.get("/submit").status_code == 200
    assert stored(db_engine) == other
    no_leak(db_engine, caplog, "password123", old, other)


def test_two_logins_in_a_row(client, ann, db_engine):
    set_hash(db_engine, OLD.hash("password123"))
    assert login(client, password="password123").status_code == 303
    first = stored(db_engine)
    client.post("/logout")
    assert login(client, password="password123").status_code == 303
    assert stored(db_engine) == first
