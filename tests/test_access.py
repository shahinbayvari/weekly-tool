import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.access import NotAllowed, not_allowed_handler, require_login, require_owner
from app.auth import router as auth_router
from app.main import settings
from app.users import create_user

PW = "correct horse"


def make_test_app() -> FastAPI:
    """A separate app: the two test routes never touch the real app."""
    test_app = FastAPI()
    test_app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="weekly_session")
    test_app.add_exception_handler(NotAllowed, not_allowed_handler)
    test_app.include_router(auth_router)

    @test_app.get("/member-area")
    def member_area(user=Depends(require_login)):
        return {"email": user.email}

    @test_app.get("/owner-area")
    def owner_area(user=Depends(require_owner)):
        return {"email": user.email}

    return test_app


@pytest.fixture
def client(db_engine):
    with TestClient(make_test_app(), follow_redirects=False) as c:
        yield c


@pytest.fixture
def users(db_engine):
    return {
        "owner": create_user("olga@example.com", PW, "owner", engine=db_engine),
        "member": create_user("mia@example.com", PW, "member", engine=db_engine),
    }


EMAIL = {"owner": "olga@example.com", "member": "mia@example.com"}


def sign_in_as(client, who):
    r = client.post("/login", data={"email": EMAIL[who], "password": PW})
    assert r.status_code == 303


@pytest.mark.parametrize("path", ["/member-area", "/owner-area"])
def test_signed_out_redirects_to_login(client, users, path):
    r = client.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == f"/login?next=%2F{path[1:]}"


def test_member_on_member_route(client, users):
    sign_in_as(client, "member")
    r = client.get("/member-area")
    assert r.status_code == 200 and r.json() == {"email": "mia@example.com"}


def test_member_on_owner_route_is_403_not_redirect(client, users):
    sign_in_as(client, "member")
    r = client.get("/owner-area")
    assert r.status_code == 403
    assert "location" not in r.headers
    assert r.text == "Not allowed"
    assert r.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize("path", ["/member-area", "/owner-area"])
def test_owner_on_both_routes(client, users, path):
    sign_in_as(client, "owner")
    r = client.get(path)
    assert r.status_code == 200 and r.json() == {"email": "olga@example.com"}


@pytest.mark.parametrize("path", ["/member-area", "/owner-area"])
def test_deleted_user_cookie_counts_as_signed_out(client, users, db_engine, path):
    sign_in_as(client, "owner")
    assert client.get(path).status_code == 200
    with db_engine.begin() as c:
        c.execute(text("DELETE FROM users WHERE id = :i"), {"i": users["owner"].id})
    r = client.get(path)
    assert r.status_code == 303
    assert r.headers["location"] == f"/login?next=%2F{path[1:]}"


def test_real_app_has_no_test_routes():
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert "/member-area" not in paths and "/owner-area" not in paths
