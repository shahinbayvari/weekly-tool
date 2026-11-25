"""The owner's missing-submitters page. Shows only who has not submitted, never counts or item data."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Engine

from app.access import require_owner
from app.auth import get_engine, templates
from app.config import Settings
from app.cycle import Schedule, describe
from app.missing import missing_submitters
from app.models import User
from app.pages.submit import TIME_FORMAT, get_now, get_settings

router = APIRouter()


@router.get("/missing")
def missing_page(
    request: Request,
    user: User = Depends(require_owner),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    cycle = describe(now, Schedule.from_settings(settings))
    context = {"email": user.email, "is_open": cycle.is_open, "deadline": None, "people": []}
    if cycle.is_open:
        context["deadline"] = f"{cycle.week_close.strftime(TIME_FORMAT)} ({settings.timezone})"
        context["people"] = [m.email for m in missing_submitters(cycle.week_id, engine=engine)]
    return templates.TemplateResponse(request, "missing.html", context)
