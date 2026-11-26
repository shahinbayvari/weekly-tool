"""Login does one argon2 verification whenever email and password are non-empty (counted, not timed)."""

import argon2
import pytest
from sqlalchemy import text

import app.passwords as passwords
from app.main import app
from app.users import create_user
from tests.conftest import CsrfClient

PW = "correct horse"
MESSAGE = "Invalid email or password"


@pytest.fixture
def client(db_engine):
    with CsrfClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def ann(db_engine):
    return create_user("ann@example.com", PW, "member", engine=db_engine)


@pytest.fixture
def calls(monkeypatch):
    """Records the hash passed to every _hasher.verify call (and still does the real work)."""
    seen = []
    real = passwords._hasher

    class Spy:  # PasswordHasher uses __slots__, so swap the module's hasher for a wrapper
        def verify(self, password_hash, password):
            seen.append(password_hash)
            return real.verify(password_hash, password)

        def hash(self, *args, **kwargs):
            return real.hash(*args, **kwargs)

    monkeypatch.setattr(passwords, "_hasher", Spy())
    return seen


def login(client, email="ann@example.com", password=PW):
    return client.post("/login", data={"email": email, "password": password})


def test_unknown_email_one_verify_against_dummy(client, ann, calls):
    r = login(client, "nobody@example.com", "whatever pw")
    assert r.status_code == 401 and MESSAGE in r.text
    assert calls == [passwords.DUMMY_HASH]


def test_wrong_password_one_verify_against_users_hash(client, ann, calls):
    r = login(client, password="wrong password")
    assert r.status_code == 401 and MESSAGE in r.text
    assert calls == [ann.password_hash]


def test_correct_password_one_verify(client, ann, calls):
    r = login(client)
    assert r.status_code == 303
    assert calls == [ann.password_hash]


def test_unknown_and_wrong_have_equal_counts(client, ann, calls):
    login(client, "nobody@example.com", "whatever pw")
    unknown = len(calls)
    calls.clear()
    login(client, password="wrong password")
    assert unknown == len(calls) == 1


@pytest.mark.parametrize(
    "email,password",
    [("", "whatever pw"), ("ann@example.com", ""), ("", ""), ("   ", "whatever pw"), ("nobody@example.com", "")],
)
def test_empty_fields_do_no_verification(client, ann, calls, email, password):
    r = login(client, email, password)
    assert r.status_code == 401 and MESSAGE in r.text
    assert calls == []


def test_no_fields_do_no_verification(client, ann, calls):
    assert client.post("/login", data={}).status_code == 401
    assert calls == []


def test_dummy_hash_computed_once_and_shared(client, ann, calls, monkeypatch):
    def no_hash(*a, **k):
        raise AssertionError("hash called during a login request")

    monkeypatch.setattr(passwords._hasher, "hash", no_hash)  # the Spy from `calls`
    login(client, "nobody@example.com", "pw one one")
    login(client, "other@example.com", "pw two two")
    assert calls == [passwords.DUMMY_HASH, passwords.DUMMY_HASH]


def test_dummy_hash_has_same_parameters_as_real_hashes():
    real = passwords.hash_password(PW)
    assert argon2.extract_parameters(passwords.DUMMY_HASH) == argon2.extract_parameters(real)


def test_dummy_hash_is_valid_and_mismatches():
    with pytest.raises(argon2.exceptions.VerifyMismatchError):
        passwords._hasher.verify(passwords.DUMMY_HASH, "not the secret")


@pytest.mark.parametrize("candidate", ["", "password", PW, "x" * 128])
def test_nothing_matches_dummy_hash(candidate):
    assert passwords.verify_password(candidate, passwords.DUMMY_HASH) is False
    assert passwords.verify_unknown_user(candidate) is False


def test_unknown_email_never_logs_in(client, ann):
    r = login(client, "nobody@example.com", PW)
    assert r.status_code == 401 and MESSAGE in r.text
    assert "set-cookie" not in r.headers or "session" not in r.headers["set-cookie"]


@pytest.mark.parametrize("bad_hash", ["not-a-hash", ""])
def test_malformed_stored_hash_is_clean_401(client, ann, db_engine, bad_hash):
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE users SET password_hash = :h"), {"h": bad_hash})
    r = login(client)
    assert r.status_code == 401 and MESSAGE in r.text
