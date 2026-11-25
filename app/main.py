from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine
from starlette.middleware.sessions import SessionMiddleware

from app.access import NotAllowed, not_allowed_handler
from app.auth import get_engine, get_optional_user, router as auth_router, templates
from app.config import load_settings
from app.models import User
from app.setup import no_users, router as setup_router

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
