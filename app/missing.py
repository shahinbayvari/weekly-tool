"""Who has not submitted anything in a given week.

`missing_submitters(week)` returns every user (members and the owner) without a
single feedback item in that week, sorted by email, as a list of `Missing`
(user_id, email). It only checks whether items exist (NOT EXISTS on author and
week) and never reads the `anonymous` column, so the result is identical for
named and anonymous items. Invited people without an account are not users and
so are not listed. Reused by the reminder job (#19).
"""

from dataclasses import dataclass

from sqlalchemy import Engine, exists, select
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
