"""Login, logout and the `current_user` dependency.

The session is Starlette's signed-cookie SessionMiddleware (added in app.main);
it holds only the user id.
"""

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import User
from app.passwords import verify_password
from app.security import csrf_context
from app.users import find_user_by_email

SESSION_KEY = "user_id"

templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates"),
    context_processors=[csrf_context],  # every template gets `csrf_token`
)
router = APIRouter()


def get_engine() -> Iterator[Engine]:
    """Engine for WEEKLY_DB_PATH, disposed after the request. Tests may override it."""
    engine = make_engine()
    try:
        yield engine
    finally:
        engine.dispose()


def safe_next(value: str | None) -> str:
    """`value` if it is a relative path starting with a single '/', else '/'.

    Rejects absolute URLs, '//host', and backslash or control-character tricks
    (browsers treat a backslash like '/', and strip tabs/newlines inside URLs).
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    return value


def get_optional_user(request: Request, engine: Engine = Depends(get_engine)) -> User | None:
    """The signed-in user, or None (no/invalid cookie, or the user no longer exists)."""
    user_id = request.session.get(SESSION_KEY)
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return None
    with Session(engine, expire_on_commit=False) as session:
        return session.get(User, user_id)


def current_user(request: Request, user: User | None = Depends(get_optional_user)) -> User:
    """The signed-in user, or a 303 redirect to /login?next=<path>."""
    if user is None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={quote(target, safe='')}"})
    return user


def _form(request: Request, email: str, next_url: str, status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"email": email, "next": next_url, "error": status_code == 401},
        status_code=status_code,
    )


@router.get("/login")
def login_form(request: Request, next: str = "", user: User | None = Depends(get_optional_user)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return _form(request, "", safe_next(next))


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    engine: Engine = Depends(get_engine),
):
    next_url = safe_next(next)
    user = find_user_by_email(email, engine=engine)
    # empty fields, unknown email and wrong password all give the same response
    if not email.strip() or not password or user is None or not verify_password(password, user.password_hash):
        return _form(request, email, next_url, status_code=401)
    request.session.clear()
    request.session[SESSION_KEY] = user.id
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
