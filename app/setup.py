"""One-time owner setup: creates the owner user and the single project while no users exist."""

import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine, exists, insert, select
from sqlalchemy.exc import IntegrityError

from app.auth import SESSION_KEY, get_engine, templates
from app.config import Settings
from app.models import Project, User
from app.pages.submit import get_settings
from app.passwords import WeakPasswordError, hash_password
from app.users import InvalidDisplayNameError, InvalidEmailError, normalise_email, validate_display_name, validate_email

MAX_PROJECT_NAME_LENGTH = 100

router = APIRouter()


def no_users(engine: Engine) -> bool:
    with engine.connect() as conn:
        return not conn.scalar(select(exists().where(User.id.is_not(None))))


def require_setup_open(engine: Engine = Depends(get_engine)) -> None:
    if not no_users(engine):
        raise HTTPException(status_code=404)


def token_matches(submitted: str, configured: str) -> bool:
    """Constant-time check on UTF-8 bytes. Surrounding whitespace (as pasted) is ignored."""
    return hmac.compare_digest(submitted.strip().encode("utf-8"), configured.encode("utf-8"))


def _form(
    request: Request,
    project_name: str,
    email: str,
    error: str | None = None,
    token_required: bool = False,
    display_name: str = "",
):
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "project_name": project_name,
            "email": email,
            "display_name": display_name,
            "error": error,
            "token_required": token_required,
        },
        status_code=400 if error else 200,
    )


@router.get("/setup", dependencies=[Depends(require_setup_open)])
def setup_form(request: Request, settings: Settings = Depends(get_settings)):
    return _form(request, "", "", token_required=bool(settings.setup_token))


@router.post("/setup", dependencies=[Depends(require_setup_open)])
def setup(
    request: Request,
    project_name: str = Form(""),
    email: str = Form(""),
    display_name: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    setup_token: str = Form(""),
    engine: Engine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
):
    token_required = bool(settings.setup_token)
    if token_required and not token_matches(setup_token, settings.setup_token):
        return _form(request, project_name.strip(), normalise_email(email), "Setup token is incorrect", True, display_name.strip())
    name = project_name.strip()
    normalised = normalise_email(email)
    error = None
    try:
        validate_email(normalised)
    except InvalidEmailError:
        error = "Enter a valid email address"
    if error is None and not name:
        error = "Project name is required"
    if error is None and len(name) > MAX_PROJECT_NAME_LENGTH:
        error = f"Project name must be at most {MAX_PROJECT_NAME_LENGTH} characters"
    if error is None:
        try:
            display_name = validate_display_name(display_name)
        except InvalidDisplayNameError as exc:
            error = str(exc)
    password_hash = None
    if error is None:
        try:
            password_hash = hash_password(password)
        except WeakPasswordError as exc:
            error = str(exc)
    if error is None and password != confirm_password:
        error = "Passwords do not match"
    if error:
        return _form(request, name, normalised, error, token_required, display_name.strip())

    # One write transaction: BEGIN IMMEDIATE takes the SQLite write lock before the
    # "no users yet" re-check, so only one of several concurrent submissions passes it.
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if conn.scalar(select(exists().where(User.id.is_not(None)))):
                raise HTTPException(status_code=404)
            user_id = conn.execute(
                insert(User).values(email=normalised, password_hash=password_hash, role="owner", display_name=display_name)
            ).inserted_primary_key[0]
            conn.execute(insert(Project).values(id=1, name=name))
            conn.commit()
        except IntegrityError:
            conn.rollback()
            raise HTTPException(status_code=404) from None
        except BaseException:
            conn.rollback()
            raise

    request.session.clear()
    request.session[SESSION_KEY] = user_id
    return RedirectResponse("/", status_code=303)
