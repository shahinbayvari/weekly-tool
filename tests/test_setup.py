import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Project, User
from app.users import create_user

PW = "correct horse"


@pytest.fixture
def client(db_engine):
    with TestClient(app, follow_redirects=False) as c:
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
    home = client.get("/")
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
    assert r.status_code == 303 and client.get("/").status_code == 200


def test_concurrent_submissions_exactly_one_wins(db_engine):
    # file-backed database (db_engine) and real threads: all pass the early check
    # and race for the write lock
    n = 4
    barrier = threading.Barrier(n)
    results = []

    def submit(i):
        with TestClient(app, follow_redirects=False) as c:
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
