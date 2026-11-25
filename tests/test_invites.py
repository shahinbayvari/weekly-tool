import hashlib
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.users import create_user

PW = "correct horse"
EMAIL = {"owner": "olga@example.com", "member": "mia@example.com"}


@pytest.fixture
def client(db_engine):
    create_user(EMAIL["owner"], PW, "owner", engine=db_engine)
    create_user(EMAIL["member"], PW, "member", engine=db_engine)
    with TestClient(app, follow_redirects=False) as c:
        yield c


def sign_in(client, who="owner"):
    assert client.post("/login", data={"email": EMAIL[who], "password": PW}).status_code == 303


@pytest.fixture
def owner(client):
    sign_in(client)
    return client


def invite(client, email):
    return client.post("/invites", data={"email": email})


def rows(db_engine):
    with db_engine.connect() as c:
        return c.execute(
            text("SELECT id, email, token_hash, invited_by, accepted_at, revoked_at FROM invites ORDER BY id")
        ).all()


def token_of(response):
    m = re.search(r"/invite/([A-Za-z0-9_-]+)", response.text)
    assert m
    return m.group(1)


def test_signed_out_redirects(client):
    r = client.get("/invites")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Finvites"
    assert client.post("/invites", data={"email": "a@b.c"}).status_code == 303
    assert client.post("/invites/1/revoke").status_code == 303


def test_member_forbidden(client, db_engine):
    sign_in(client, "member")
    assert client.get("/invites").status_code == 403
    assert invite(client, "x@example.com").status_code == 403
    assert client.post("/invites/1/revoke").status_code == 403
    assert rows(db_engine) == []


def test_owner_sees_form_and_empty_sections(owner):
    r = owner.get("/invites")
    assert r.status_code == 200
    assert 'name="email"' in r.text
    assert "Pending invites" in r.text and "Accepted invites" in r.text
    assert r.text.count("<p>None</p>") == 2


def test_create_shows_link_once_and_stores_only_hash(owner, db_engine):
    r = invite(owner, "  New@Example.COM ")
    assert r.status_code == 200
    token = token_of(r)
    assert f"http://testserver/invite/{token}" in r.text
    assert len(token) == 43  # token_urlsafe(32)
    (row,) = rows(db_engine)
    assert row.email == "new@example.com"
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(tuple(row))
    assert row.invited_by == 1
    later = owner.get("/invites")
    assert token not in later.text and "/invite/" not in later.text
    assert "new@example.com" in later.text


def test_link_uses_request_base_url(owner):
    r = owner.post("/invites", data={"email": "a@example.com"}, headers={"host": "127.0.0.1:8000"})
    assert "http://127.0.0.1:8000/invite/" in r.text


@pytest.mark.parametrize("bad", ["", "   ", "no-at-sign"])
def test_invalid_email_rejected(owner, db_engine, bad):
    r = invite(owner, bad)
    assert r.status_code == 400 and "valid email" in r.text
    assert rows(db_engine) == []


@pytest.mark.parametrize("email", ["mia@example.com", " MIA@Example.com ", "olga@example.com", "OLGA@EXAMPLE.COM "])
def test_existing_user_rejected(owner, db_engine, email):
    r = invite(owner, email)
    assert r.status_code == 400 and "already exists" in r.text
    assert "revoke" not in r.text.lower()
    assert rows(db_engine) == []


@pytest.mark.parametrize("again", ["new@example.com", " NEW@example.com ", "New@Example.Com"])
def test_pending_duplicate_rejected(owner, db_engine, again):
    invite(owner, "new@example.com")
    r = invite(owner, again)
    assert r.status_code == 400
    assert "pending invite" in r.text and "revoke it first" in r.text
    assert len(rows(db_engine)) == 1


def test_lists_in_separate_sections_newest_first(owner, db_engine):
    for e in ("a@example.com", "b@example.com", "c@example.com", "d@example.com"):
        invite(owner, e)
    with db_engine.begin() as c:
        c.execute(text("UPDATE invites SET created_at='2026-01-01 00:00:00' WHERE email='a@example.com'"))
        c.execute(text("UPDATE invites SET created_at='2026-03-01 00:00:00' WHERE email='b@example.com'"))
        c.execute(text("UPDATE invites SET created_at='2026-02-01 00:00:00' WHERE email='c@example.com'"))
        c.execute(text("UPDATE invites SET created_at='2026-06-01 00:00:00' WHERE email='d@example.com'"))
        c.execute(text("UPDATE invites SET accepted_at='2026-04-01 00:00:00' WHERE email='a@example.com'"))
        c.execute(text("UPDATE invites SET accepted_at='2026-05-01 00:00:00' WHERE email='c@example.com'"))
    body = owner.get("/invites").text
    pending_part, accepted_part = body.split("Accepted invites")
    assert pending_part.index("d@example.com") < pending_part.index("b@example.com")
    assert "2026-06-01" in pending_part and "2026-03-01" in pending_part
    assert "a@example.com" not in pending_part and "c@example.com" not in pending_part
    assert accepted_part.index("c@example.com") < accepted_part.index("a@example.com")
    assert "2026-05-01" in accepted_part and "2026-04-01" in accepted_part
    assert "b@example.com" not in accepted_part and "d@example.com" not in accepted_part
    assert pending_part.count("Revoke") == 2 and "Revoke" not in accepted_part


def test_revoke_then_reinvite(owner, db_engine):
    invite(owner, "new@example.com")
    r = owner.post("/invites/1/revoke")
    assert r.status_code == 303 and r.headers["location"] == "/invites"
    assert rows(db_engine)[0].revoked_at is not None
    page = owner.get("/invites")
    assert "new@example.com" not in page.text and page.text.count("<p>None</p>") == 2
    r = invite(owner, "new@example.com")
    assert r.status_code == 200 and "/invite/" in r.text
    assert len(rows(db_engine)) == 2


def test_revoke_unknown_or_already_revoked_is_404(owner):
    assert owner.post("/invites/999/revoke").status_code == 404
    invite(owner, "new@example.com")
    owner.post("/invites/1/revoke")
    assert owner.post("/invites/1/revoke").status_code == 404


def test_accepted_cannot_be_revoked_or_reinvited(owner, db_engine):
    invite(owner, "new@example.com")
    with db_engine.begin() as c:
        c.execute(text("UPDATE invites SET accepted_at=CURRENT_TIMESTAMP"))
    create_user("new@example.com", PW, "member", engine=db_engine)
    assert owner.post("/invites/1/revoke").status_code == 400
    assert rows(db_engine)[0].revoked_at is None
    r = invite(owner, "new@example.com")
    assert r.status_code == 400 and "already exists" in r.text
    assert "new@example.com" in owner.get("/invites").text
