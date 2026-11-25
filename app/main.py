import logging
import re

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine
from starlette.middleware.sessions import SessionMiddleware

from app.access import NotAllowed, not_allowed_handler
from app.auth import get_engine, get_optional_user, router as auth_router, templates
from app.config import load_settings
from app.invites import router as invites_router
from app.models import User
from app.setup import no_users, router as setup_router



class RedactInviteToken(logging.Filter):
    """Keep invite tokens out of server access logs (uvicorn logs the request path)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                re.sub(r"/invite/[^\s?#]+", "/invite/[redacted]", a) if isinstance(a, str) else a for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(RedactInviteToken())

settings = load_settings()  # fail at import on invalid configuration

app = FastAPI(title="Weekly Feedback Tool")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="weekly_session",
    same_site="lax",  # HttpOnly is always set by SessionMiddleware
    https_only=settings.env == "production",
)
app.add_exception_handler(NotAllowed, not_allowed_handler)
app.include_router(auth_router)
app.include_router(setup_router)
app.include_router(invites_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index(
    request: Request,
    user: User | None = Depends(get_optional_user),
    engine: Engine = Depends(get_engine),
):
    if user is None:
        return RedirectResponse("/setup" if no_users(engine) else "/login?next=%2F", status_code=303)
    return templates.TemplateResponse(request, "index.html", {"email": user.email})
