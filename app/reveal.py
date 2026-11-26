"""Reveal a week's feedback after its deadline, with anonymous authors unrecoverable.

A week is identified by the local date its window opens (see app.cycle), so the
identifier must be a real ``YYYY-MM-DD`` date that falls on the configured open
day; anything else raises ValueError.

Anonymity is enforced by construction: named items are read with a join to the
users table (for the display name, never the email); anonymous items are read by a separate query that
selects only text and category, so no author data is ever loaded for them. The
two lists are then merged and sorted in Python by (text case-insensitively, text
exactly, anonymous-before-named, display name), which never uses author, id or time, so
a member's anonymous items are not next to their named ones. Among identical
texts every anonymous item comes before every named one (a fixed rule, not tied
to any author); named duplicates are ordered by the name shown on them.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.config import Settings, load_settings
from app.cycle import Schedule, _window
from app.db import make_engine
from app.models import FeedbackItem, User

_WEEK_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class NamedItem:
    text: str
    category: str
    display_name: str


@dataclass(frozen=True)
class AnonymousItem:
    text: str
    category: str


@dataclass(frozen=True)
class Reveal:
    """`continue` is a Python keyword, so that list is `continue_` (or `by_category("continue")`)."""

    revealed: bool
    start: tuple[NamedItem | AnonymousItem, ...] = ()
    stop: tuple[NamedItem | AnonymousItem, ...] = ()
    continue_: tuple[NamedItem | AnonymousItem, ...] = ()

    def by_category(self, category: str) -> tuple[NamedItem | AnonymousItem, ...]:
        if category not in ("start", "stop", "continue"):
            raise ValueError("Unknown category")
        return self.continue_ if category == "continue" else getattr(self, category)


def _parse_week(week: str, schedule: Schedule) -> date:
    if not isinstance(week, str) or not _WEEK_RE.fullmatch(week):
        raise ValueError("week must be a YYYY-MM-DD date")
    try:
        day = date.fromisoformat(week)
    except ValueError:
        raise ValueError("week must be a real YYYY-MM-DD date") from None
    if day.weekday() != schedule.open_day:
        raise ValueError("week must be the date its window opens")
    return day


def _sort_key(item: NamedItem | AnonymousItem) -> tuple:
    named = isinstance(item, NamedItem)
    return (item.text.casefold(), item.text, named, item.display_name if named else "")


def reveal_week(
    week: str,
    now: datetime,
    *,
    engine: Engine | None = None,
    settings: Settings | Schedule | None = None,
) -> Reveal:
    """The week's items grouped by category once its deadline has passed (now >= deadline).

    Before that (window open or not yet opened) returns revealed=False with no items
    and does not touch the database. Raises ValueError for a naive `now` or an
    invalid week identifier.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if settings is None:
        settings = load_settings()
    schedule = settings if isinstance(settings, Schedule) else Schedule.from_settings(settings)
    _, deadline = _window(schedule, _parse_week(week, schedule))
    if now < deadline:
        return Reveal(revealed=False)

    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine) as session:
            named = session.execute(
                select(FeedbackItem.text, FeedbackItem.category, User.display_name)
                .join(User, User.id == FeedbackItem.author_id)
                .where(FeedbackItem.week == week, FeedbackItem.anonymous.is_(False))
            ).all()
            anonymous = session.execute(
                select(FeedbackItem.text, FeedbackItem.category).where(
                    FeedbackItem.week == week, FeedbackItem.anonymous.is_(True)
                )
            ).all()
    finally:
        if own_engine:
            engine.dispose()

    combined = [NamedItem(t, c, e) for t, c, e in named] + [AnonymousItem(t, c) for t, c in anonymous]
    combined.sort(key=_sort_key)
    grouped = {c: tuple(i for i in combined if i.category == c) for c in ("start", "stop", "continue")}
    return Reveal(True, grouped["start"], grouped["stop"], grouped["continue"])
