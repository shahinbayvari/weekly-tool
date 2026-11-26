import logging
import re
import threading

import pytest
from tests.conftest import CsrfClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import app.invites as inv
from app.invites import hash_token
from app.main import app
from app.models import Invite, User
from app.users import create_user

PW = "correct horse"
NEW_PW = "brand new pass"
INVITEE = "new@example.com"
NOT_VALID = "This invite link is not valid. Ask the owner for a new one."


@pytest.fixture
def owner_id(db_engine):
    return create_user("olga@example.com", PW, "owner", display_name="Olga Owner", engine=db_engine).id


@pytest.fixture
def client(db_engine, owner_id):
    with CsrfClient(app, follow_redirects=False) as c:
        yield c


def add_invite(engine, owner_id, token="tok-valid-123", email=INVITEE, **cols):
    with Session(engine) as s:
        s.add(Invite(email=email, token_hash=hash_token(token), invited_by=owner_id, **cols))
        s.commit()
    return token


def form(pw=NEW_PW, confirm=None, name="Nina New"):
    return {"display_name": name, "password": pw, "confirm_password": pw if confirm is None else confirm}


def users(engine):
    with Session(engine) as s:
        return [(u.email, u.role) for u in s.scalars(select(User).order_by(User.id))]


def names(engine):
    with Session(engine) as s:
        return [u.display_name for u in s.scalars(select(User).order_by(User.id))]


def accepted_at(engine, token):
    with Session(engine) as s:
        return s.scalar(select(Invite.accepted_at).where(Invite.token_hash == hash_token(token)))


def assert_not_valid(r, token=None):
    assert r.status_code == 404
    assert NOT_VALID in r.text
    assert "<form" not in r.text and "<input" not in r.text
    if token:
        assert token not in r.text


def test_get_valid_shows_form_with_readonly_email(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    r = client.get(f"/invite/{t}")
    assert r.status_code == 200
    assert INVITEE in r.text and "readonly" in r.text
    assert 'name="password"' in r.text and 'name="confirm_password"' in r.text
    assert 'name="email"' not in r.text  # the email is not a submitted field
    field = re.search(r'<input[^>]*name="display_name"[^>]*>', r.text).group(0)
    assert 'type="text"' in field and 'maxlength="50"' in field and " required" in field
    assert r.text.index("email_display") < r.text.index('name="display_name"') < r.text.index('name="password"')


def test_get_unknown_used_revoked_give_identical_404(client, db_engine, owner_id):
    used = add_invite(db_engine, owner_id, "used-tok", "a@example.com", accepted_at=func.current_timestamp())
    revoked = add_invite(db_engine, owner_id, "revoked-tok", "b@example.com", revoked_at=func.current_timestamp())
    pages = []
    for t in ("unknown-tok", used, revoked):
        r = client.get(f"/invite/{t}")
        assert_not_valid(r, t)
        pages.append(r.text)
    assert len(set(pages)) == 1


def test_post_valid_creates_member_accepts_signs_in_redirects(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    assert accepted_at(db_engine, t) is None
    r = client.post(f"/invite/{t}", data=form())
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert users(db_engine) == [("olga@example.com", "owner"), (INVITEE, "member")]
    assert names(db_engine)[-1] == "Nina New"
    assert accepted_at(db_engine, t) is not None
    home = client.get("/submit")
    assert home.status_code == 200 and "Signed in as Nina New" in home.text and INVITEE not in home.text


def test_post_ignores_submitted_email(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    client.post(f"/invite/{t}", data={**form(), "email": "evil@example.com"})
    assert users(db_engine)[-1] == (INVITEE, "member")


@pytest.mark.parametrize(
    "pw,confirm,msg", [("short", "short", "at least"), (NEW_PW, "different pass", "do not match")]
)
def test_bad_password_reshows_form_and_consumes_nothing(client, db_engine, owner_id, pw, confirm, msg):
    t = add_invite(db_engine, owner_id)
    r = client.post(f"/invite/{t}", data=form(pw, confirm))
    assert r.status_code == 400 and msg in r.text and 'name="password"' in r.text
    assert users(db_engine) == [("olga@example.com", "owner")]
    assert accepted_at(db_engine, t) is None
    assert client.get("/").status_code == 303  # not signed in
    assert client.post(f"/invite/{t}", data=form()).status_code == 303  # link still works


def test_reuse_after_acceptance_is_not_valid(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    client.post(f"/invite/{t}", data=form())
    with CsrfClient(app, follow_redirects=False) as other:
        assert_not_valid(other.get(f"/invite/{t}"), t)
        assert_not_valid(other.post(f"/invite/{t}", data=form()), t)
    assert len(users(db_engine)) == 2


def test_post_unknown_and_revoked_are_not_valid(client, db_engine, owner_id):
    revoked = add_invite(db_engine, owner_id, "revoked-tok", revoked_at=func.current_timestamp())
    assert_not_valid(client.post("/invite/unknown-tok", data=form()), "unknown-tok")
    assert_not_valid(client.post(f"/invite/{revoked}", data=form()), revoked)
    assert len(users(db_engine)) == 1


def test_existing_user_with_email_at_submit_time(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    create_user(INVITEE, PW, "member", engine=db_engine)  # appears after the invite was made
    assert_not_valid(client.post(f"/invite/{t}", data=form()), t)
    assert users(db_engine).count((INVITEE, "member")) == 1
    assert accepted_at(db_engine, t) is None


def test_existing_user_appearing_between_check_and_write(client, db_engine, owner_id, monkeypatch):
    # the user is created after the early check but before the locked re-check
    t = add_invite(db_engine, owner_id)
    real = inv.hash_password

    def sneaky(pw):
        create_user(INVITEE, PW, "member", engine=db_engine)
        return real(pw)

    monkeypatch.setattr(inv, "hash_password", sneaky)
    assert_not_valid(client.post(f"/invite/{t}", data=form()), t)
    assert users(db_engine).count((INVITEE, "member")) == 1
    assert accepted_at(db_engine, t) is None


def test_signed_in_visitor_redirected_and_invite_unused(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    assert client.post("/login", data={"email": "olga@example.com", "password": PW}).status_code == 303
    r = client.get(f"/invite/{t}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.post(f"/invite/{t}", data=form())
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert users(db_engine) == [("olga@example.com", "owner")]
    assert accepted_at(db_engine, t) is None
    assert "Signed in as Olga Owner" in client.get("/submit").text  # still signed in as the owner


def test_concurrent_submissions_exactly_one_wins(db_engine, owner_id):
    # file-backed database (db_engine) and real threads racing for the write lock
    t = add_invite(db_engine, owner_id)
    n = 4
    barrier = threading.Barrier(n)
    results = []

    def submit():
        with CsrfClient(app, follow_redirects=False) as c:
            barrier.wait()
            r = c.post(f"/invite/{t}", data=form())
            results.append((r.status_code, NOT_VALID in r.text))

    threads = [threading.Thread(target=submit) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == [(303, False)] + [(404, True)] * (n - 1)
    assert users(db_engine) == [("olga@example.com", "owner"), (INVITEE, "member")]
    assert accepted_at(db_engine, t) is not None


def test_token_not_in_server_logs(caplog):
    # uvicorn logs the request path; the filter installed by app.main must redact the token
    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/invite/SECRETTOKEN-abc?x=1", "1.1", 404)
    assert "SECRETTOKEN-abc" not in caplog.text and "/invite/[redacted]?x=1" in caplog.text


def test_token_not_in_app_logs_or_error_pages(client, db_engine, owner_id, caplog):
    add_invite(db_engine, owner_id, "SECRETTOKEN-abc")
    path = "/invite/SECRETTOKEN-abc"
    with caplog.at_level(logging.DEBUG):
        weak = client.post(path, data=form("short"))
        client.post(path, data=form())
        with CsrfClient(app, follow_redirects=False) as other:
            again_get = other.get(path)
            again_post = other.post(path, data=form())
    # (httpx's own client-side request log is excluded: it is the test client, not the app)
    app_logs = " ".join(r.getMessage() for r in caplog.records if r.name != "httpx")
    assert "SECRETTOKEN-abc" not in app_logs
    # a re-shown form carries it only in its action URL; the not-valid pages never do
    assert weak.text.count("SECRETTOKEN-abc") == 1 and f'action="{path}"' in weak.text
    assert_not_valid(again_get, "SECRETTOKEN-abc")
    assert_not_valid(again_post, "SECRETTOKEN-abc")


def test_accepted_invite_creates_a_current_hash(client, db_engine, owner_id):
    from app.passwords import needs_rehash

    t = add_invite(db_engine, owner_id)
    client.post(f"/invite/{t}", data=form())
    with Session(db_engine) as s:
        h = s.scalar(select(User.password_hash).where(User.email == INVITEE))
    assert needs_rehash(h) is False


# --- display name (#29) ---------------------------------------------------------


@pytest.mark.parametrize(
    "name,message",
    [
        ("", "Enter a name"),
        ("   ", "Enter a name"),
        ("\u200b", "Enter a name"),
        ("x" * 51, "Name must be at most 50 characters"),
        ("Sam\nLee", "Name must be a single line"),
        ("Sam\tLee", "Name must be a single line"),
        ("Sam\r\nLee", "Name must be a single line"),
        ("Sam\u2028Lee", "Name must be a single line"),
        ("Sam\x00Lee", "Name must be a single line"),
        ("anonymous", "Choose a different name"),
        (" ANONYMOUS ", "Choose a different name"),
        ("AnOnYmOuS", "Choose a different name"),
    ],
)
def test_bad_name_reshows_form_and_consumes_nothing(client, db_engine, owner_id, name, message):
    t = add_invite(db_engine, owner_id)
    r = client.post(f"/invite/{t}", data=form(name=name))
    assert r.status_code == 400 and message in r.text and 'name="display_name"' in r.text
    assert users(db_engine) == [("olga@example.com", "owner")]
    assert accepted_at(db_engine, t) is None
    assert client.get("/").status_code == 303  # not signed in
    assert client.post(f"/invite/{t}", data=form()).status_code == 303  # link still works


def test_name_is_trimmed_and_inner_spaces_kept(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    assert client.post(f"/invite/{t}", data=form(name="  Sam   Lee  ")).status_code == 303
    assert names(db_engine)[-1] == "Sam   Lee"


def test_name_of_50_accepted_51_rejected(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    assert client.post(f"/invite/{t}", data=form(name="y" * 51)).status_code == 400
    assert client.post(f"/invite/{t}", data=form(name="y" * 50)).status_code == 303
    assert names(db_engine)[-1] == "y" * 50


def test_emoji_and_non_latin_name_intact(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    assert client.post(f"/invite/{t}", data=form(name="Zoë 李雷 \U0001f600")).status_code == 303
    assert names(db_engine)[-1] == "Zoë 李雷 \U0001f600"


def test_duplicate_names_are_allowed(client, db_engine, owner_id):
    t1 = add_invite(db_engine, owner_id, "tok-1", "one@example.com")
    t2 = add_invite(db_engine, owner_id, "tok-2", "two@example.com")
    assert client.post(f"/invite/{t1}", data=form(name="Sam")).status_code == 303
    with CsrfClient(app, follow_redirects=False) as other:
        assert other.post(f"/invite/{t2}", data=form(name="Sam")).status_code == 303
    assert names(db_engine)[1:] == ["Sam", "Sam"]


def test_owner_may_share_a_name_with_a_member(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    assert client.post(f"/invite/{t}", data=form(name="Olga Owner")).status_code == 303


def test_errors_keep_the_entered_name_escaped_and_not_the_password(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    r = client.post(f"/invite/{t}", data=form("short", name="  <b>x</b>  "))
    assert r.status_code == 400 and "at least" in r.text
    assert 'value="&lt;b&gt;x&lt;/b&gt;"' in r.text and "<b>x</b>" not in r.text
    r = client.post(f"/invite/{t}", data=form(confirm="different pass", name="Nina"))
    assert 'value="Nina"' in r.text and "different pass" not in r.text
    r = client.post(f"/invite/{t}", data=form(name="x" * 51))
    assert 'value="' + "x" * 51 + '"' in r.text
    assert f'value="{INVITEE}"' in r.text


def test_name_error_comes_before_password_errors(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    r = client.post(f"/invite/{t}", data=form("short", name=""))
    assert "Enter a name" in r.text and "at least" not in r.text


def test_missing_name_field_is_rejected(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    r = client.post(f"/invite/{t}", data={"password": NEW_PW, "confirm_password": NEW_PW})
    assert r.status_code == 400 and "Enter a name" in r.text
    assert len(users(db_engine)) == 1


def test_bad_name_on_a_used_link_is_still_just_not_valid(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id, accepted_at=func.current_timestamp())
    assert_not_valid(client.post(f"/invite/{t}", data=form(name="")), t)


def test_new_member_signs_in_with_email_not_name(client, db_engine, owner_id):
    t = add_invite(db_engine, owner_id)
    client.post(f"/invite/{t}", data=form(name="Nina New"))
    client.post("/logout")
    assert client.post("/login", data={"email": "Nina New", "password": NEW_PW}).status_code == 401
    assert client.post("/login", data={"email": INVITEE, "password": NEW_PW}).status_code == 303
