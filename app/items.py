"""Add, edit, delete and list feedback items. Service layer only; no web routes.

No clock rules here (the window is #12). Only an item's author may change it.
"""

from datetime import datetime, timezone

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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


def _owned_item(session: Session, item_id: int, user_id: int) -> FeedbackItem:
    item = session.get(FeedbackItem, item_id)
    if item is None:
        raise ItemNotFoundError("Item not found")
    if item.author_id != user_id:
        raise NotAuthorError("Only the author can change this item")
    return item


def add_item(
    user_id: int,
    week: str,
    category: str,
    text: str,
    anonymous: bool = False,
    *,
    engine: Engine | None = None,
) -> FeedbackItem:
    """Store and return a new item. Everything is validated before anything is written."""
    if category not in CATEGORIES:
        raise ValueError(f"Category must be one of: {', '.join(CATEGORIES)}")
    trimmed = _validate_text(text)

    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            now = _now()
            item = FeedbackItem(
                author_id=user_id,
                week=week,
                category=category,
                text=trimmed,
                anonymous=bool(anonymous),
                created_at=now,
                updated_at=now,
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


def edit_item(item_id: int, user_id: int, text: str, *, engine: Engine | None = None) -> FeedbackItem:
    """Change the text (same rules as add_item) and updated_at; the author only."""
    trimmed = _validate_text(text)
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            item = _owned_item(session, item_id, user_id)
            item.text = trimmed
            item.updated_at = _now()
            session.commit()
            return item
    finally:
        if own_engine:
            engine.dispose()


def delete_item(item_id: int, user_id: int, *, engine: Engine | None = None) -> None:
    """Remove the item; the author only."""
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine) as session:
            session.delete(_owned_item(session, item_id, user_id))
            session.commit()
    finally:
        if own_engine:
            engine.dispose()


def set_anonymous(item_id: int, user_id: int, anonymous: bool, *, engine: Engine | None = None) -> FeedbackItem:
    """Set the anonymous flag to `anonymous`; the author only. Idempotent."""
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            item = _owned_item(session, item_id, user_id)
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
