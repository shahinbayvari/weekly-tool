"""Who has not submitted anything in a given week.

`missing_submitters(week)` returns every user (members and the owner) without a
single feedback item in that week, sorted by email, as a list of `Missing`
(user_id, email). It only checks whether items exist (NOT EXISTS on author and
week) and never reads the `anonymous` column, so the result is identical for
named and anonymous items. Invited people without an account are not users and
so are not listed. Reused by the reminder job (#19).
"""

from dataclasses import dataclass

from sqlalchemy import Engine, exists, func, select
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import FeedbackItem, User


@dataclass(frozen=True)
class Missing:
    user_id: int
    email: str


def missing_query(week: str):
    has_item = exists().where(FeedbackItem.author_id == User.id, FeedbackItem.week == week)
    return select(User.id, User.email).where(~has_item).order_by(User.email)


def missing_submitters(week: str, *, engine: Engine | None = None) -> list[Missing]:
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine) as session:
            return [Missing(i, e) for i, e in session.execute(missing_query(week)).all()]
    finally:
        if own_engine:
            engine.dispose()


# Anonymity safeguard (see _docs/anonymity.md): with fewer than this many
# submitters (but at least one) the owner only gets a count, never names.
MIN_SUBMITTERS_FOR_NAMES = 3


@dataclass(frozen=True)
class MissingNames:
    """Full list form. Never empty-by-safeguard; empty means everyone submitted."""

    people: tuple[Missing, ...]


@dataclass(frozen=True)
class MissingCount:
    """Count-only form. Deliberately carries no names."""

    count: int


def visible_missing(week: str, *, engine: Engine | None = None) -> MissingNames | MissingCount:
    """Apply the small-team safeguard on top of `missing_submitters`.

    S = users with at least one item (total users - missing), derived without
    reading the anonymous column. S == 0 or S >= MIN_SUBMITTERS_FOR_NAMES gives
    the names; 1 <= S < MIN_SUBMITTERS_FOR_NAMES gives only the count (unless
    nobody is missing, which reveals nothing).
    """
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        missing = missing_submitters(week, engine=engine)
        with Session(engine) as session:
            total = session.execute(select(func.count()).select_from(User)).scalar_one()
    finally:
        if own_engine:
            engine.dispose()
    submitters = total - len(missing)
    if missing and 0 < submitters < MIN_SUBMITTERS_FOR_NAMES:
        return MissingCount(len(missing))
    return MissingNames(tuple(missing))
