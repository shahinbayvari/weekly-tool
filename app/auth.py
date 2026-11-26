"""Login, logout and the `current_user` dependency.

The session is Starlette's signed-cookie SessionMiddleware (added in app.main);
it holds only the user id.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import Project, User
from app.passwords import hash_existing_password, needs_rehash, verify_password, verify_unknown_user
from app.security import csrf_context
from app.users import find_user_by_email, update_password_hash_if_unchanged

logger = logging.getLogger("app.auth")
SESSION_KEY = "user_id"

DEFAULT_PROJECT_NAME = "Weekly Feedback"
MEMBER_LINKS = (("Submit", "/submit"), ("Reveal", "/reveal"))
OWNER_LINKS = MEMBER_LINKS + (("Invites", "/invites"), ("Missing", "/missing"))


@dataclass(frozen=True)
class Layout:
    """What templates/base.html shows: the project name and, when signed in, the user and nav."""

    project_name: str = DEFAULT_PROJECT_NAME
    display_name: str | None = None
    links: tuple[tuple[str, str, bool], ...] = ()  # (label, href, is_current_page)


def build_layout(request: Request, public: bool = False) -> Layout:
    """Layout for this request. `public` pages (login, setup, invites) never show the user or nav."""
    name, user = DEFAULT_PROJECT_NAME, None
    engine = make_engine()
    try:
        with Session(engine) as session:
            project = session.scalar(select(Project))
            if project is not None and project.name.strip():
                name = project.name
            user_id = request.session.get(SESSION_KEY)
            if not public and isinstance(user_id, int) and not isinstance(user_id, bool):
                user = session.get(User, user_id)
    except SQLAlchemyError:  # e.g. database not migrated yet: still render the page, as signed out
        logger.exception("could not read the project and user for the page header")
        name, user = DEFAULT_PROJECT_NAME, None
    finally:
        engine.dispose()
    if user is None:
        return Layout(project_name=name)
    path = request.url.path.rstrip("/") or "/"
    links = OWNER_LINKS if user.role == "owner" else MEMBER_LINKS
    return Layout(name, user.display_name, tuple((label, href, href == path) for label, href in links))


def layout_context(request: Request) -> dict:
    """Jinja2 context processor: `layout(public)` is looked up lazily by base.html only,
    so fragments (HTMX responses) never touch the database for it."""
    return {"layout": lambda public=False: build_layout(request, public)}


templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates"),
    context_processors=[csrf_context, layout_context],  # every template gets `csrf_token` and `layout`
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


def _rehash_if_needed(user: User, password: str, engine: Engine) -> None:
    """After a successful verification: replace an outdated hash by one with the current parameters.

    Never breaks the login: any failure is logged (user id only, no password or hash) and ignored.
    """
    try:
        if not needs_rehash(user.password_hash):
            return
        new_hash = hash_existing_password(password)
        update_password_hash_if_unchanged(user.id, user.password_hash, new_hash, engine=engine)
    except Exception:  # noqa: BLE001 - a rehash problem must never fail the login
        logger.warning("could not rehash the password of user %s", user.id)


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    engine: Engine = Depends(get_engine),
):
    next_url = safe_next(next)
    # Empty fields are rejected before any hashing. Otherwise exactly one verification runs
    # (against a dummy hash for an unknown email), so all failures do the same work and
    # give the same response.
    if not email.strip() or not password:
        return _form(request, email, next_url, status_code=401)
    user = find_user_by_email(email, engine=engine)
    if user is None:
        verify_unknown_user(password)
        return _form(request, email, next_url, status_code=401)
    if not verify_password(password, user.password_hash):
        return _form(request, email, next_url, status_code=401)
    _rehash_if_needed(user, password, engine)
    request.session.clear()
    request.session[SESSION_KEY] = user.id
    return RedirectResponse(next_url, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
