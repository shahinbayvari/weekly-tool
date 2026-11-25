"""The submission page: a member manages their own feedback for the current week.

Every change goes through the item functions in app.items. The page is updated in
place with HTMX: handlers return HTML fragments. Validation errors are returned as
a normal 200 fragment next to the field; a closed window is a 200 response that
swaps nothing in the page but fills the #notice element (HX-Reswap: none), because
HTMX does not swap 4xx/5xx responses by default.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import Engine

from app.access import require_login
from app.auth import get_engine, templates
from app.config import Settings, load_settings
from app.cycle import TIME_FORMAT, Schedule, describe
from app.items import (
    CATEGORIES,
    InvalidTextError,
    ItemNotFoundError,
    NotAuthorError,
    WindowClosedError,
    add_item,
    delete_item,
    edit_item,
    list_items,
    set_anonymous,
)
from app.models import FeedbackItem, User

router = APIRouter()

TITLES = {"start": "Start", "stop": "Stop", "continue": "Continue"}
CLOSED_MESSAGE = "The window is closed"


def get_now() -> datetime:
    """The current UTC time. The only place the real clock is read; tests override it."""
    return datetime.now(timezone.utc)


def get_settings() -> Settings:
    """Application settings. Tests may override it."""
    return load_settings()


def _category(category: str) -> str:
    if category not in CATEGORIES:
        raise HTTPException(status_code=404)
    return category


def _fragment(request: Request, name: str, context: dict, notice: str = ""):
    return templates.TemplateResponse(request, name, {**context, "notice": notice, "titles": TITLES})


def _closed(request: Request):
    """200 so HTMX processes it; nothing is swapped in place, only the notice is shown."""
    response = _fragment(request, "_notice_only.html", {}, notice=CLOSED_MESSAGE)
    response.headers["HX-Reswap"] = "none"
    return response


def _own_item(user: User, item_id: int, week: str, engine: Engine) -> FeedbackItem:
    for items in list_items(user.id, week, engine=engine).values():
        for item in items:
            if item.id == item_id:
                return item
    raise HTTPException(status_code=404)


def _week(now: datetime, settings: Settings):
    return describe(now, Schedule.from_settings(settings))


def _service_error(exc: Exception):
    if isinstance(exc, NotAuthorError):
        return PlainTextResponse("Not allowed", status_code=403)
    return PlainTextResponse("Not found", status_code=404)


@router.get("/submit")
def submit_page(
    request: Request,
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    cycle = _week(now, settings)
    items = list_items(user.id, cycle.week_id, engine=engine)
    return templates.TemplateResponse(
        request,
        "submit.html",
        {
            "email": user.email,
            "is_open": cycle.is_open,
            "deadline": cycle.week_close.strftime(TIME_FORMAT),
            "next_open": cycle.next_open.strftime(TIME_FORMAT),
            "tz": settings.timezone,
            "sections": [(c, TITLES[c], items[c]) for c in CATEGORIES],
            "titles": TITLES,
            "notice": "",
        },
    )


@router.post("/submit/{category}/items")
def add(
    request: Request,
    category: str,
    text: str = Form(""),
    anonymous: str | None = Form(None),
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    _category(category)
    wants_anonymous = anonymous is not None
    try:
        add_item(user.id, now, category, text, wants_anonymous, engine=engine, settings=settings)
    except WindowClosedError:
        return _closed(request)
    except InvalidTextError as exc:
        form = {"category": category, "text": text, "anonymous": wants_anonymous, "error": str(exc)}
        return _fragment(request, "_add_response.html", {"form": form, "refresh_list": False})
    cycle = _week(now, settings)
    items = list_items(user.id, cycle.week_id, engine=engine)[category]
    form = {"category": category, "text": "", "anonymous": False, "error": ""}
    return _fragment(
        request, "_add_response.html", {"form": form, "refresh_list": True, "category": category, "items": items}
    )


@router.get("/submit/items/{item_id}")
def show_item(
    request: Request,
    item_id: int,
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    item = _own_item(user, item_id, _week(now, settings).week_id, engine)
    return _fragment(request, "_item_response.html", {"item": item})


@router.get("/submit/items/{item_id}/edit")
def edit_form(
    request: Request,
    item_id: int,
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    cycle = _week(now, settings)
    item = _own_item(user, item_id, cycle.week_id, engine)
    if not cycle.is_open:
        return _closed(request)
    return _fragment(request, "_item_edit_response.html", {"item": item, "text": item.text, "error": ""})


@router.post("/submit/items/{item_id}/edit")
def save_edit(
    request: Request,
    item_id: int,
    text: str = Form(""),
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    try:
        item = edit_item(item_id, user.id, now, text, engine=engine, settings=settings)
    except WindowClosedError:
        return _closed(request)
    except InvalidTextError as exc:
        # Validation runs before the ownership check in the service, so check it here:
        # an item that is not the user's own (or does not exist) is a 404.
        item = _own_item(user, item_id, _week(now, settings).week_id, engine)
        return _fragment(request, "_item_edit_response.html", {"item": item, "text": text, "error": str(exc)})
    except (NotAuthorError, ItemNotFoundError) as exc:
        return _service_error(exc)
    return _fragment(request, "_item_response.html", {"item": item})


@router.post("/submit/items/{item_id}/anonymous")
def toggle_anonymous(
    request: Request,
    item_id: int,
    anonymous: str = Form("false"),
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    try:
        item = set_anonymous(item_id, user.id, now, anonymous == "true", engine=engine, settings=settings)
    except WindowClosedError:
        return _closed(request)
    except (NotAuthorError, ItemNotFoundError) as exc:
        return _service_error(exc)
    return _fragment(request, "_item_response.html", {"item": item})


@router.post("/submit/items/{item_id}/delete")
def delete(
    request: Request,
    item_id: int,
    user: User = Depends(require_login),
    engine: Engine = Depends(get_engine),
    now: datetime = Depends(get_now),
    settings: Settings = Depends(get_settings),
):
    # Read first: the item's category tells us which list to re-render. Another
    # member's item is not in this list, so the service call below decides (403).
    cycle = _week(now, settings)
    mine = {i.id: c for c, its in list_items(user.id, cycle.week_id, engine=engine).items() for i in its}
    try:
        delete_item(item_id, user.id, now, engine=engine, settings=settings)
    except WindowClosedError:
        return _closed(request)
    except (NotAuthorError, ItemNotFoundError) as exc:
        return _service_error(exc)
    category = mine.get(item_id)
    items = list_items(user.id, cycle.week_id, engine=engine)[category] if category else []
    return _fragment(request, "_list_response.html", {"category": category, "items": items})
