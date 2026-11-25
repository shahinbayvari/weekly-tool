"""Reminder job logic: email each member who has not submitted shortly before the deadline.

`send_reminders(now, sender, settings)` is meant to be called repeatedly (the
scheduler is #20). Sending is possible from max(window open, deadline - lead)
until the deadline (exclusive). Each (user, week) gets at most one reminder.

Design choices
- A reminder is recorded in `reminders_sent` only AFTER `sender.send` succeeded,
  so a failed send is retried by the next run.
- Failures: any Exception from the sender for one member is caught, logged as a
  warning with the user id only (no address, no body, no recipient list), and the
  run continues. The returned count excludes failures.
- Concurrency: the unique (user_id, week) constraint is the last line of
  defence. Because the record is written after sending (as required), two runs
  that overlap in the same instant could both send to one member; the scheduler
  (#20) must not overlap runs. We re-check the table right before each send to
  shrink that window, and a unique-constraint violation when recording is handled
  (the other run already recorded it) instead of crashing; that email is not counted.
- Uses the FULL `missing_submitters` list (owner included), never `visible_missing`.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.cycle import Schedule, describe, format_moment
from app.db import make_engine
from app.email import EmailSender
from app.missing import missing_submitters
from app.models import ReminderSent

log = logging.getLogger(__name__)


def build_message(deadline: datetime, settings: Settings) -> tuple[str, str]:
    """Subject and body. Identical for everyone: nothing about other members."""
    when = format_moment(deadline, settings)
    subject = f"Reminder: submit your feedback by {when}"
    body = (
        "You have not added any feedback for this week yet.\n"
        f"The window closes on {when}.\n"
        f"Add your feedback here: {settings.base_url}/submit\n"
    )
    return subject, body


def _already_reminded(session: Session, user_id: int, week: str) -> bool:
    return (
        session.execute(
            select(ReminderSent.id).where(ReminderSent.user_id == user_id, ReminderSent.week == week)
        ).first()
        is not None
    )


def send_reminders(now: datetime, sender: EmailSender, settings: Settings, *, engine: Engine | None = None) -> int:
    """Send due reminders and return how many emails were sent (failures excluded)."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cycle = describe(now, Schedule.from_settings(settings))
    if not cycle.is_open:
        return 0
    instant = now.astimezone(UTC)
    deadline = cycle.week_close.astimezone(UTC)  # UTC arithmetic: no wall-clock/DST surprises
    start = max(cycle.week_open.astimezone(UTC), deadline - timedelta(hours=settings.reminder_hours_before))
    if not (start <= instant < deadline):
        return 0

    week = cycle.week_id
    subject, body = build_message(cycle.week_close, settings)
    own_engine = engine is None
    engine = engine or make_engine()
    sent = 0
    try:
        for person in missing_submitters(week, engine=engine):
            with Session(engine) as session:
                if _already_reminded(session, person.user_id, week):
                    continue
                try:
                    sender.send(person.email, subject, body)
                except Exception as exc:
                    log.warning(
                        "reminder for user %s week %s failed: %s", person.user_id, week, type(exc).__name__
                    )
                    continue
                session.add(ReminderSent(user_id=person.user_id, week=week))
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()  # a concurrent run recorded it first
                    log.warning("reminder for user %s week %s was already recorded", person.user_id, week)
                    continue
                sent += 1
    finally:
        if own_engine:
            engine.dispose()
    return sent
