"""Owner-only invites: create (link shown once), list, revoke.

Only the SHA-256 of the token is stored (a fast, deterministic hash so #9 can
look an invite up by the token in its link). The plain token exists only in the
response to the POST that created it.
"""

import hashlib
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.access import require_owner
from app.auth import get_engine, templates
from app.models import Invite, User
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
