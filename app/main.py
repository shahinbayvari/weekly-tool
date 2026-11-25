import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.access import NotAllowed, not_allowed_handler
from app.auth import get_engine, get_optional_user, router as auth_router
from app.config import load_settings
from app.errors import http_exception_handler
from app.invites import router as invites_router
from app.models import User
from app.pages.missing import router as missing_router
from app.pages.reveal import router as reveal_router
from app.pages.submit import get_now, get_settings, router as submit_router  # noqa: F401
from app.scheduler import start_scheduler, stop_scheduler
from app.security import install as install_security
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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    started = start_scheduler(settings)  # no-op when WEEKLY_SCHEDULER_ENABLED=false
    try:
        yield
    finally:
        if started:
            stop_scheduler()


app = FastAPI(title="Weekly Feedback Tool", lifespan=lifespan)
install_security(app, settings)
app.add_exception_handler(NotAllowed, not_allowed_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.include_router(auth_router)
app.include_router(setup_router)
app.include_router(invites_router)
app.include_router(submit_router)
app.include_router(reveal_router)
app.include_router(missing_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index(
    user: User | None = Depends(get_optional_user),
    engine: Engine = Depends(get_engine),
):
    if user is None:
        return RedirectResponse("/setup" if no_users(engine) else "/login?next=%2F", status_code=303)
    return RedirectResponse("/submit", status_code=303)
