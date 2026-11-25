from fastapi import Depends, FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

from app.auth import current_user, router as auth_router, templates
from app.config import load_settings
from app.models import User

settings = load_settings()  # fail at import on invalid configuration

app = FastAPI(title="Weekly Feedback Tool")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="weekly_session",
    same_site="lax",  # HttpOnly is always set by SessionMiddleware
    https_only=settings.env == "production",
)
app.include_router(auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(request, "index.html", {"email": user.email})
