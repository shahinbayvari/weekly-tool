"""Owner-only invites: create (link shown once), list, revoke.

Only the SHA-256 of the token is stored (a fast, deterministic hash so #9 can
look an invite up by the token in its link). The plain token exists only in the
response to the POST that created it.
"""

import hashlib
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import require_owner
from app.auth import SESSION_KEY, get_engine, get_optional_user, templates
from app.models import Invite, User
from app.passwords import WeakPasswordError, hash_password
from app.users import InvalidEmailError, normalise_email, validate_email

router = APIRouter()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _page(request: Request, engine: Engine, *, email: str = "", error: str | None = None, link: str | None = None):
    with Session(engine) as session:
        pending = session.scalars(
            select(Invite)
            .where(Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))
            .order_by(Invite.created_at.desc(), Invite.id.desc())
        ).all()
        accepted = session.scalars(
            select(Invite).where(Invite.accepted_at.is_not(None)).order_by(Invite.accepted_at.desc(), Invite.id.desc())
        ).all()
        context = {"email": email, "error": error, "link": link, "pending": pending, "accepted": accepted}
        return templates.TemplateResponse(request, "invites.html", context, status_code=400 if error else 200)


@router.get("/invites")
def invites_page(request: Request, user: User = Depends(require_owner), engine: Engine = Depends(get_engine)):
    return _page(request, engine)


@router.post("/invites")
def create_invite(
    request: Request,
    email: str = Form(""),
    user: User = Depends(require_owner),
    engine: Engine = Depends(get_engine),
):
    normalised = normalise_email(email)
    try:
        validate_email(normalised)
    except InvalidEmailError:
        return _page(request, engine, email=normalised, error="Enter a valid email address")

    token = secrets.token_urlsafe(32)
    with Session(engine) as session:
        if session.scalar(select(User.id).where(User.email == normalised)):
            error = "A user with this email already exists"
        elif session.scalar(
            select(Invite.id).where(
                Invite.email == normalised, Invite.accepted_at.is_(None), Invite.revoked_at.is_(None)
            )
        ):
            error = "A pending invite for this email already exists; revoke it first to invite again"
        else:
            error = None
            session.add(Invite(email=normalised, token_hash=hash_token(token), invited_by=user.id))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                error = "Could not create the invite, try again"
    if error:
        return _page(request, engine, email=normalised, error=error)
    link = f"{str(request.base_url).rstrip('/')}/invite/{token}"
    return _page(request, engine, link=link)


@router.post("/invites/{invite_id}/revoke")
def revoke_invite(
    invite_id: int,
    user: User = Depends(require_owner),
    engine: Engine = Depends(get_engine),
):
    with Session(engine) as session:
        invite = session.get(Invite, invite_id)
        if invite is None or invite.revoked_at is not None:
            raise HTTPException(status_code=404)
        if invite.accepted_at is not None:
            raise HTTPException(status_code=400, detail="An accepted invite cannot be revoked")
        invite.revoked_at = func.current_timestamp()
        session.commit()
    return RedirectResponse("/invites", status_code=303)


# --- accepting an invite (#9) ------------------------------------------------
# The token is never logged or echoed: the only place it appears is the form's
# action URL (the path the visitor already used). Invalid links all give the same page.


def _not_valid(request: Request):
    return templates.TemplateResponse(request, "invite_not_valid.html", {}, status_code=404)


def _open_invite_email(engine: Engine, token: str) -> str | None:
    """Email of the unused, unrevoked invite for this token, if no user has that email yet."""
    with Session(engine) as session:
        invite = session.scalar(
            select(Invite).where(
                Invite.token_hash == hash_token(token),
                Invite.accepted_at.is_(None),
                Invite.revoked_at.is_(None),
            )
        )
        if invite is None or session.scalar(select(User.id).where(User.email == invite.email)):
            return None
        return invite.email


def _accept_form(request: Request, token: str, email: str, error: str | None = None):
    context = {"token": token, "email": email, "error": error}
    return templates.TemplateResponse(request, "accept_invite.html", context, status_code=400 if error else 200)


@router.get("/invite/{token}")
def accept_invite_form(
    request: Request,
    token: str,
    user: User | None = Depends(get_optional_user),
    engine: Engine = Depends(get_engine),
):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    email = _open_invite_email(engine, token)
    if email is None:
        return _not_valid(request)
    return _accept_form(request, token, email)


@router.post("/invite/{token}")
def accept_invite(
    request: Request,
    token: str,
    password: str = Form(""),
    confirm_password: str = Form(""),
    user: User | None = Depends(get_optional_user),
    engine: Engine = Depends(get_engine),
):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    email = _open_invite_email(engine, token)
    if email is None:
        return _not_valid(request)
    error = None
    password_hash = None
    try:
        password_hash = hash_password(password)
    except WeakPasswordError as exc:
        error = str(exc)
    if error is None and password != confirm_password:
        error = "Passwords do not match"
    if error:
        return _accept_form(request, token, email, error)

    # One write transaction: BEGIN IMMEDIATE takes the SQLite write lock before the
    # re-check, so of several concurrent submissions only one still sees the invite open.
    token_hash = hash_token(token)
    with engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            invite_email = conn.scalar(
                select(Invite.email).where(
                    Invite.token_hash == token_hash, Invite.accepted_at.is_(None), Invite.revoked_at.is_(None)
                )
            )
            if invite_email is None or conn.scalar(select(User.id).where(User.email == invite_email)):
                conn.rollback()
                return _not_valid(request)
            user_id = conn.execute(
                insert(User).values(email=invite_email, password_hash=password_hash, role="member")
            ).inserted_primary_key[0]
            conn.execute(
                update(Invite).where(Invite.token_hash == token_hash).values(accepted_at=func.current_timestamp())
            )
            conn.commit()
        except IntegrityError:
            conn.rollback()
            return _not_valid(request)
        except BaseException:
            conn.rollback()
            raise

    request.session.clear()
    request.session[SESSION_KEY] = user_id
    return RedirectResponse("/", status_code=303)
