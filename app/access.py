"""Access rules for routes: `require_login` (any signed-in user) and `require_owner`.

Both build on `current_user` from app.auth, so the session is read in one place
only. A signed-out request (including a cookie for a deleted user) gets the 303
redirect to /login?next=<path> from `current_user`; a signed-in member on an
owner-only route gets a plain 403 "Not allowed" page.
"""

from fastapi import Depends, Request
from fastapi.responses import PlainTextResponse

from app.auth import current_user
from app.models import User


class NotAllowed(Exception):
    """Raised for a signed-in user who lacks the required role."""


def not_allowed_handler(request: Request, exc: NotAllowed) -> PlainTextResponse:
    return PlainTextResponse("Not allowed", status_code=403)


def require_login(user: User = Depends(current_user)) -> User:
    """Any signed-in user; the route receives the user."""
    return user


def require_owner(user: User = Depends(require_login)) -> User:
    """Only a user with role `owner`; the route receives the user."""
    if user.role != "owner":
        raise NotAllowed()
    return user
