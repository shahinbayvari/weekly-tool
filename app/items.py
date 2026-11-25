"""Add, edit, delete and list feedback items. Service layer only; no web routes.

Writes are only allowed while the submission window is open (see app.cycle), and
an item can only be changed in the window of the week it was filed under.
Only an item's author may change it; the owner gets no exemption.
"""

from datetime import datetime, timezone

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, load_settings
from app.cycle import Cycle, Schedule, describe
from app.db import make_engine
from app.models import FeedbackItem

MAX_TEXT_LENGTH = 280
CATEGORIES = ("start", "stop", "continue")

# Every character Unicode (and str.splitlines) treats as a line boundary.
LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85  ")


class InvalidTextError(ValueError):
    """The text is empty, too long, or contains a line break."""


class NotAuthorError(PermissionError):
    """The user is not the author of the item."""


class ItemNotFoundError(LookupError):
    """No item has this id."""


class UnknownUserError(LookupError):
    """No user has this id."""


class WindowClosedError(Exception):
    """The submission window is closed (or the item belongs to a past week)."""


def _current_cycle(now: datetime, settings: Settings | Schedule | None) -> tuple[Cycle, Schedule]:
    """The cycle at `now` (ValueError if naive, checked before anything else)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if settings is None:
        settings = load_settings()
    schedule = settings if isinstance(settings, Schedule) else Schedule.from_settings(settings)
    return describe(now, schedule), schedule


def _require_open(now: datetime, settings: Settings | Schedule | None) -> Cycle:
    cycle, _ = _current_cycle(now, settings)
    if not cycle.is_open:
        opens = cycle.next_open.strftime("%A %Y-%m-%d %H:%M %Z")
        raise WindowClosedError(f"The submission window is closed. It next opens on {opens} ({cycle.next_open.tzinfo}).")
    return cycle


def _validate_text(text: str) -> str:
    """The trimmed text, or raise InvalidTextError. Otherwise the text is untouched."""
    if any(ch in LINE_BREAKS for ch in text):
        raise InvalidTextError("Text must be a single line")
    trimmed = text.strip()
    if not trimmed:
        raise InvalidTextError("Text must not be empty")
    if len(trimmed) > MAX_TEXT_LENGTH:
        raise InvalidTextError(f"Text must be at most {MAX_TEXT_LENGTH} characters")
    return trimmed


def _now() -> datetime:
    """Naive UTC with microseconds, the same convention as the table's CURRENT_TIMESTAMP."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _owned_item(session: Session, item_id: int, user_id: int, cycle: Cycle) -> FeedbackItem:
    item = session.get(FeedbackItem, item_id)
    if item is None:
        raise ItemNotFoundError("Item not found")
    if item.author_id != user_id:
        raise NotAuthorError("Only the author can change this item")
    if item.week != cycle.week_id:
        raise WindowClosedError(f"The submission window for week {item.week} is closed; this item can no longer be changed.")
    return item


def add_item(
    user_id: int,
    now: datetime,
    category: str,
    text: str,
    anonymous: bool = False,
    *,
    engine: Engine | None = None,
    settings: Settings | Schedule | None = None,
) -> FeedbackItem:
    """Store and return a new item under the current week. Everything is validated before anything is written.

    `now` must be timezone-aware. Raises WindowClosedError when the window is closed
    (checked before category and text).
    """
    cycle = _require_open(now, settings)
    if category not in CATEGORIES:
        raise ValueError(f"Category must be one of: {', '.join(CATEGORIES)}")
    trimmed = _validate_text(text)

    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            stamp = _now()
            item = FeedbackItem(
                author_id=user_id,
                week=cycle.week_id,
                category=category,
                text=trimmed,
                anonymous=bool(anonymous),
                created_at=stamp,
                updated_at=stamp,
            )
            session.add(item)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise UnknownUserError("No such user") from None
            return item
    finally:
        if own_engine:
            engine.dispose()


def edit_item(
    item_id: int,
    user_id: int,
    now: datetime,
    text: str,
    *,
    engine: Engine | None = None,
    settings: Settings | Schedule | None = None,
) -> FeedbackItem:
    """Change the text (same rules as add_item) and updated_at; the author only, in the item's own open window."""
    cycle = _require_open(now, settings)
    trimmed = _validate_text(text)
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            item = _owned_item(session, item_id, user_id, cycle)
            item.text = trimmed
            item.updated_at = _now()
            session.commit()
            return item
    finally:
        if own_engine:
            engine.dispose()


def delete_item(
    item_id: int,
    user_id: int,
    now: datetime,
    *,
    engine: Engine | None = None,
    settings: Settings | Schedule | None = None,
) -> None:
    """Remove the item; the author only, in the item's own open window."""
    cycle = _require_open(now, settings)
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine) as session:
            session.delete(_owned_item(session, item_id, user_id, cycle))
            session.commit()
    finally:
        if own_engine:
            engine.dispose()


def set_anonymous(
    item_id: int,
    user_id: int,
    now: datetime,
    anonymous: bool,
    *,
    engine: Engine | None = None,
    settings: Settings | Schedule | None = None,
) -> FeedbackItem:
    """Set the anonymous flag to `anonymous`; the author only, in the item's own open window. Idempotent."""
    cycle = _require_open(now, settings)
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            item = _owned_item(session, item_id, user_id, cycle)
            item.anonymous = bool(anonymous)
            session.commit()
            return item
    finally:
        if own_engine:
            engine.dispose()


def list_items(user_id: int, week: str, *, engine: Engine | None = None) -> dict[str, list[FeedbackItem]]:
    """This user's items for `week`, as {category: [oldest first]} with every category present."""
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            rows = session.scalars(
                select(FeedbackItem)
                .where(FeedbackItem.author_id == user_id, FeedbackItem.week == week)
                .order_by(FeedbackItem.created_at, FeedbackItem.id)
            ).all()
        grouped: dict[str, list[FeedbackItem]] = {c: [] for c in CATEGORIES}
        for item in rows:
            grouped[item.category].append(item)
        return grouped
    finally:
        if own_engine:
            engine.dispose()
