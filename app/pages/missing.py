"""The owner's missing-submitters page. Shows who has not submitted (or only a count for tiny teams), never item data."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Engine

from app.access import require_owner
from app.auth import get_engine, templates
from app.config import Settings
from app.cycle import Schedule, describe
from app.missing import MissingCount, visible_missing
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
    context = {"email": user.email, "is_open": cycle.is_open, "deadline": None, "people": [], "count": None}
    if cycle.is_open:
        context["deadline"] = f"{cycle.week_close.strftime(TIME_FORMAT)} ({settings.timezone})"
        result = visible_missing(cycle.week_id, engine=engine)
        if isinstance(result, MissingCount):
            context["count"] = result.count
        else:
            context["people"] = [m.email for m in result.people]
    return templates.TemplateResponse(request, "missing.html", context)
