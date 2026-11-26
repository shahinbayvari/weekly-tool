import logging
import re
import threading
from pathlib import Path

import pytest
from tests.conftest import CsrfClient, strip_csrf
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import app
from app.pages.submit import get_settings
from app.models import Project, User
from app.users import create_user

PW = "correct horse"


@pytest.fixture
def client(db_engine):
    with CsrfClient(app, follow_redirects=False) as c:
        yield c


def data(**over):
    d = {"project_name": "Team A", "email": "Owner@Example.com", "password": PW, "confirm_password": PW}
    d.update(over)
    return d


def counts(engine):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(User)), s.scalar(select(func.count()).select_from(Project))


def test_form_shown_with_no_users(client):
    r = client.get("/setup")
    assert r.status_code == 200
    for field in ("project_name", "email", "password", "confirm_password"):
        assert f'name="{field}"' in r.text


def test_success_creates_owner_and_project_and_signs_in(client, db_engine):
    r = client.post("/setup", data=data(project_name="  Team A  "))
    assert r.status_code == 303 and r.headers["location"] == "/"
    with Session(db_engine) as s:
        users = s.scalars(select(User)).all()
        projects = s.scalars(select(Project)).all()
    assert [(u.email, u.role) for u in users] == [("owner@example.com", "owner")]
    assert [(p.id, p.name) for p in projects] == [(1, "Team A")]
    home = client.get("/submit")
    assert home.status_code == 200 and "owner@example.com" in home.text


def test_login_works_and_home_redirects_to_setup_without_users(client):
    assert client.get("/login").status_code == 200
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/setup"


def test_home_redirects_to_login_when_users_exist(client, db_engine):
    create_user("a@example.com", PW, "member", engine=db_engine)
    assert client.get("/").headers["location"].startswith("/login")


def test_setup_404_once_a_user_exists(client, db_engine):
    create_user("a@example.com", PW, "member", engine=db_engine)
    assert client.get("/setup").status_code == 404
    assert client.post("/setup", data=data()).status_code == 404
    assert counts(db_engine) == (1, 0)


def test_setup_404_after_setup_and_nothing_changes(client, db_engine):
    assert client.post("/setup", data=data()).status_code == 303
    assert client.get("/setup").status_code == 404
    assert client.post("/setup", data=data(project_name="Other", email="x@example.com")).status_code == 404
    assert counts(db_engine) == (1, 1)
    with Session(db_engine) as s:
        assert s.get(Project, 1).name == "Team A"


@pytest.mark.parametrize(
    "over, message",
    [
        ({"password": "short", "confirm_password": "short"}, "at least 8"),
        ({"password": "x" * 129, "confirm_password": "x" * 129}, "at most 128"),
        ({"confirm_password": "different pass"}, "do not match"),
        ({"email": "not-an-email"}, "valid email"),
        ({"email": "   "}, "valid email"),
        ({"project_name": "   "}, "Project name is required"),
        ({"project_name": "x" * 101}, "at most 100"),
    ],
)
def test_invalid_input_reshows_form_and_creates_nothing(client, db_engine, over, message):
    r = client.post("/setup", data=data(**over))
    assert r.status_code == 400
    assert message in r.text
    assert 'name="project_name"' in r.text
    assert counts(db_engine) == (0, 0)
    assert client.get("/").headers["location"] == "/setup"  # not signed in


def test_error_keeps_email_and_project_name_but_not_password(client):
    r = client.post("/setup", data=data(password="weakpw", confirm_password="weakpw"))
    assert 'value="owner@example.com"' in r.text
    assert 'value="Team A"' in r.text
    assert "weakpw" not in r.text
    r = client.post("/setup", data=data(confirm_password="mismatch!!"))
    assert PW not in r.text and "mismatch!!" not in r.text


def test_project_name_of_100_chars_accepted(client, db_engine):
    assert client.post("/setup", data=data(project_name=" " + "x" * 100 + " ")).status_code == 303
    assert counts(db_engine) == (1, 1)


def test_owner_is_a_regular_user_who_can_sign_out_and_in(client):
    client.post("/setup", data=data())
    assert client.post("/logout").status_code == 303
    r = client.post("/login", data={"email": "owner@example.com", "password": PW})
    assert r.status_code == 303 and client.get("/submit").status_code == 200


def test_concurrent_submissions_exactly_one_wins(db_engine):
    # file-backed database (db_engine) and real threads: all pass the early check
    # and race for the write lock
    n = 4
    barrier = threading.Barrier(n)
    results = []

    def submit(i):
        with CsrfClient(app, follow_redirects=False) as c:
            body = data(email=f"owner{i}@example.com", project_name=f"Project {i}")
            barrier.wait()
            results.append(c.post("/setup", data=body).status_code)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [303] + [404] * (n - 1)
    assert counts(db_engine) == (1, 1)
    with Session(db_engine) as s:
        assert [u.role for u in s.scalars(select(User))] == ["owner"]


def test_setup_creates_a_current_hash(client, db_engine):
    from app.passwords import needs_rehash

    client.post("/setup", data=data())
    with Session(db_engine) as s:
        assert needs_rehash(s.scalar(select(User.password_hash))) is False


# --- setup token (WEEKLY_SETUP_TOKEN) ------------------------------------------

TOKEN = "correct-setup-token-123"
WRONG = "wrong-setup-token-1234"
TOKEN_ERROR = "Setup token is incorrect"
TOKEN_INPUT = re.compile(r'<input[^>]*name="setup_token"[^>]*>')


@pytest.fixture
def tok(db_engine):
    """Client for an app whose settings carry a setup token."""
    app.dependency_overrides[get_settings] = lambda: Settings(setup_token=TOKEN)
    try:
        with CsrfClient(app, follow_redirects=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_token_field_shown_only_when_configured(tok, client):
    page = tok.get("/setup")
    assert page.status_code == 200
    for field in ("project_name", "email", "password", "confirm_password"):
        assert f'name="{field}"' in page.text
    m = TOKEN_INPUT.search(page.text)
    assert m and 'type="password"' in m.group(0) and 'autocomplete="off"' in m.group(0)
    assert "value=" not in m.group(0)
    assert TOKEN not in page.text
    app.dependency_overrides.pop(get_settings)
    assert 'name="setup_token"' not in client.get("/setup").text


def test_query_token_has_no_effect_on_get(tok):
    page = tok.get(f"/setup?setup_token={TOKEN}")
    assert page.status_code == 200 and TOKEN not in page.text
    assert "value=" not in TOKEN_INPUT.search(page.text).group(0)


@pytest.mark.parametrize("over", [{}, {"setup_token": ""}, {"setup_token": WRONG}, {"setup_token": "  \t\n "}])
def test_missing_empty_wrong_or_blank_token_rejected(tok, db_engine, over):
    r = tok.post("/setup", data=data(**over))
    assert r.status_code == 400 and TOKEN_ERROR in r.text
    assert counts(db_engine) == (0, 0)
    assert tok.get("/submit").status_code != 200  # not signed in


def test_missing_and_wrong_give_the_same_message(tok):
    missing = tok.post("/setup", data=data())
    wrong = tok.post("/setup", data=data(setup_token=WRONG))
    assert missing.status_code == wrong.status_code == 400
    assert strip_csrf(missing.text) == strip_csrf(wrong.text)


def test_token_error_keeps_name_and_email_and_hides_secrets(tok):
    r = tok.post("/setup", data=data(setup_token=WRONG))
    assert 'value="owner@example.com"' in r.text and 'value="Team A"' in r.text
    assert WRONG not in r.text and TOKEN not in r.text and PW not in r.text
    assert "value=" not in TOKEN_INPUT.search(r.text).group(0)


def test_token_checked_before_other_fields(tok):
    r = tok.post("/setup", data=data(setup_token=WRONG, email="not-an-email", password="x", confirm_password="y"))
    assert r.status_code == 400 and TOKEN_ERROR in r.text
    assert "valid email" not in r.text and "do not match" not in r.text


def test_token_checked_before_hashing(tok, monkeypatch):
    import app.setup as setup_mod

    def boom(_):
        raise AssertionError("hash_password ran before the token check")

    monkeypatch.setattr(setup_mod, "hash_password", boom)
    assert tok.post("/setup", data=data(setup_token=WRONG)).status_code == 400


def test_correct_token_creates_owner_and_signs_in(tok, db_engine):
    r = tok.post("/setup", data=data(setup_token=TOKEN))
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert counts(db_engine) == (1, 1)
    assert "owner@example.com" in tok.get("/submit").text


@pytest.mark.parametrize("typed", [f" {TOKEN}", f"{TOKEN}\n", f"\t{TOKEN} \r\n"])
def test_surrounding_whitespace_is_accepted(tok, db_engine, typed):
    assert tok.post("/setup", data=data(setup_token=typed)).status_code == 303
    assert counts(db_engine) == (1, 1)


def test_whitespace_inside_token_is_wrong(tok, db_engine):
    typed = TOKEN[:5] + " " + TOKEN[5:]
    r = tok.post("/setup", data=data(setup_token=typed))
    assert r.status_code == 400 and counts(db_engine) == (0, 0)


@pytest.mark.parametrize(
    "typed", ["x" * 5000, "tést-töken-中文-1234567", TOKEN + "é", "\U0001F600" * 20]
)
def test_long_or_non_ascii_token_is_just_incorrect(tok, db_engine, typed):
    r = tok.post("/setup", data=data(setup_token=typed))
    assert r.status_code == 400 and TOKEN_ERROR in r.text
    assert counts(db_engine) == (0, 0)


def test_token_only_accepted_from_form_body(tok, db_engine):
    r = tok.post(f"/setup?setup_token={TOKEN}", data=data())
    assert r.status_code == 400 and TOKEN_ERROR in r.text
    assert counts(db_engine) == (0, 0)


def test_correct_token_with_invalid_form_shows_that_error_and_clears_token(tok, db_engine):
    r = tok.post("/setup", data=data(setup_token=TOKEN, confirm_password="mismatch!!"))
    assert r.status_code == 400 and "Passwords do not match" in r.text
    assert TOKEN not in r.text and "mismatch!!" not in r.text
    assert "value=" not in TOKEN_INPUT.search(r.text).group(0)
    assert counts(db_engine) == (0, 0)


def test_no_token_configured_ignores_a_sent_token(client, db_engine):
    r = client.post("/setup", data=data(setup_token=WRONG))
    assert r.status_code == 303 and counts(db_engine) == (1, 1)


@pytest.mark.parametrize("sent", [{}, {"setup_token": TOKEN}, {"setup_token": WRONG}])
def test_404_once_a_user_exists_whatever_the_token(tok, db_engine, sent):
    create_user("a@example.com", PW, "member", engine=db_engine)
    assert tok.get("/setup").status_code == 404
    assert tok.get(f"/setup?setup_token={TOKEN}").status_code == 404
    assert tok.post("/setup", data=data(**sent)).status_code == 404
    assert counts(db_engine) == (1, 0)


def test_double_submit_with_token(tok, db_engine):
    assert tok.post("/setup", data=data(setup_token=TOKEN)).status_code == 303
    assert tok.post("/setup", data=data(setup_token=TOKEN)).status_code == 404
    assert counts(db_engine) == (1, 1)


def test_concurrent_submissions_with_token_exactly_one_wins(db_engine):
    app.dependency_overrides[get_settings] = lambda: Settings(setup_token=TOKEN)
    n = 4
    barrier = threading.Barrier(n)
    results = []

    def submit(i):
        with CsrfClient(app, follow_redirects=False) as c:
            body = data(email=f"owner{i}@example.com", project_name=f"Project {i}", setup_token=TOKEN)
            barrier.wait()
            results.append(c.post("/setup", data=body).status_code)

    try:
        threads = [threading.Thread(target=submit, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        app.dependency_overrides.pop(get_settings, None)
    assert sorted(results) == [303] + [404] * (n - 1)
    assert counts(db_engine) == (1, 1)


def test_comparison_uses_compare_digest_on_bytes(tok, monkeypatch):
    import app.setup as setup_mod

    calls = []
    real = setup_mod.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(setup_mod.hmac, "compare_digest", spy)
    tok.post("/setup", data=data(setup_token=" " + WRONG + "\n"))
    assert (WRONG.encode(), TOKEN.encode()) in calls  # other code (CSRF) also compares digests


def test_token_never_logged_or_in_responses(tok, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = [tok.get("/setup").text, tok.post("/setup", data=data(setup_token=WRONG)).text]
    ok = tok.post("/setup", data=data(setup_token=TOKEN))
    bodies += [ok.text, tok.get("/submit").text]
    headers = "".join(f"{k}={v}" for k, v in ok.headers.items())
    for secret in (TOKEN, WRONG):
        assert all(secret not in b for b in bodies)
        assert secret not in caplog.text and secret not in headers


def test_setup_template_has_no_inline_script_or_style():
    path = Path(__file__).resolve().parent.parent / "app" / "templates" / "setup.html"
    html = path.read_text(encoding="utf-8")
    assert "<script" not in html and "<style" not in html and "style=" not in html and " onclick" not in html
