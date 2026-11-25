"""The reveal page: the latest closed week's feedback in Start, Stop and Continue columns.

Data comes only from app.reveal.reveal_week; time and settings from the get_now /
get_settings dependencies. While the window is open nothing from the current week
is read or rendered: only the deadline and the previous (closed) week.

"No earlier week yet" (only matters while the window is open): the previous week
exists when the installation was already running before that week's deadline, i.e.
the earliest user was created before it, or any feedback item exists for that week
or an earlier one. A genuine earlier week without items therefore shows three
"No items" columns, while a brand-new install shows only "No earlier results".
"""

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.access import require_login
from app.auth import get_engine, templates
from app.config import Settings
from app.cycle import Schedule, _window, describe
from app.models import FeedbackItem, User
from app.pages.submit import TIME_FORMAT, TITLES, get_now, get_settings
from app.reveal import reveal_week

router = APIRouter()

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _heading(week: str) -> str:
    day = date.fromisoformat(week)
    return f"Week of {day.day} {MONTHS[day.month - 1]} {day.year}"


def _earlier_week_exists(engine: Engine, schedule: Schedule, week: str) -> bool:
    _, deadline = _window(schedule, date.fromisoformat(week))
    with Session(engine) as session:
        first_user = session.scalar(select(func.min(User.created_at)))
        if first_user is not None and first_user.replace(tzinfo=UTC) < deadline:
            return True
        return session.scalar(select(FeedbackItem.id).where(FeedbackItem.week <= week).limit(1)) is not None


def _week_view(week: str, now: datetime, engine: Engine, settings: Settings) -> dict:
    result = reveal_week(week, now, engine=engine, settings=settings)
    return {
        "heading": _heading(week),
        "columns": [(c, TITLES[c], result.by_category(c)) for c in ("start", "stop", "continue")],
    }


@router.get("/reveal")
def reveal_page(
    request: Request,
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    schedule = Schedule.from_settings(settings)
    cycle = describe(now, schedule)
    context = {"email": user.email, "hidden_until": None, "week": None}
    if cycle.is_open:
        context["hidden_until"] = f"{cycle.week_close.strftime(TIME_FORMAT)} ({settings.timezone})"
        previous = (date.fromisoformat(cycle.week_id) - timedelta(days=7)).isoformat()
        if _earlier_week_exists(engine, schedule, previous):
            context["week"] = _week_view(previous, now, engine, settings)
    else:
        context["week"] = _week_view(cycle.week_id, now, engine, settings)
    return templates.TemplateResponse(request, "reveal.html", context)
