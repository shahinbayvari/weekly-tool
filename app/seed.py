"""Dev seed script: a demo team and sample feedback for trying and screenshotting the app.

Usage:

    uv run python -m app.seed                      # fill the current week
    uv run python -m app.seed --week 2026-11-16    # fill a specific week

The week must be a real YYYY-MM-DD date on which the configured window opens
(Monday by default), the same rule as app.reveal; anything else is an error.

Creates the project "Demo Project", the owner owner@example.com and members
member1@example.com .. member5@example.com (display names Olivia Owner, Alex, Bailey,
Casey, Dana, Eli), all with the demo password
"demo-password" (DEMO_PASSWORD). That password is public on purpose: use this
only for local demos, never on a real deployment. It prints the owner email, the
password and the week used.

It writes 14 sample items (Start/Stop/Continue; 5 anonymous, 9 named) straight
into the database, so a week whose window is closed works too. The owner and
member1..member4 author items; member5 submits nothing.

Safe to repeat: missing users and the project are created, existing ones are left
alone, and a week that already has items by the seed users is not filled again.
Running with a different week on a seeded database adds only that week's items.

It refuses (message on stderr, non-zero exit, nothing written) when WEEKLY_ENV is
production, or when the database holds any user other than the six seed users.
The production check and --week validation happen before the database is touched.
Migrations are applied before the second refusal check, so a database that was
behind or missing ends up with its schema updated (schema only) even then; if the
database file already has a users table it is inspected first, without migrating.
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session

from app.config import ConfigError, load_settings
from app.cycle import Schedule, _window, describe
from app.db import make_engine, migrate
from app.models import FeedbackItem, Project, User
from app.reveal import _parse_week
from app.users import create_user, normalise_email

DEMO_PASSWORD = "demo-password"  # public demo value, local use only
PROJECT_NAME = "Demo Project"
OWNER_EMAIL = "owner@example.com"
MEMBER_EMAILS = tuple(f"member{n}@example.com" for n in range(1, 6))
SEED_EMAILS = (OWNER_EMAIL, *MEMBER_EMAILS)
SEED_NAMES = dict(zip(SEED_EMAILS, ("Olivia Owner", "Alex", "Bailey", "Casey", "Dana", "Eli")))

# (author email, category, anonymous, text). member5 deliberately has no items.
SAMPLE_ITEMS = (
    ("member1@example.com", "start", False, "Start a short written update before each Monday planning call"),
    ("member2@example.com", "start", True, "Start rotating who runs the retro so it is not always the same person"),
    ("member3@example.com", "start", False, "Start pairing on code reviews that touch the billing module"),
    ("owner@example.com", "start", False, "Start posting the sprint goals where everyone can see them"),
    ("member4@example.com", "start", True, "Start protecting one no-meeting afternoon a week"),
    ("member1@example.com", "stop", True, "Stop adding urgent requests to the sprint without dropping something"),
    ("member2@example.com", "stop", False, "Stop scheduling meetings that overrun into lunch"),
    ("member3@example.com", "stop", True, "Stop merging on Friday afternoon, it ruins weekends"),
    ("member4@example.com", "stop", False, "Stop sending status questions in three different chat channels"),
    ("member1@example.com", "continue", False, "Continue the Thursday demo sessions, they keep us honest"),
    ("member2@example.com", "continue", True, "Continue being open about mistakes in the incident reviews"),
    ("member3@example.com", "continue", False, "Continue the quick morning stand-up, ten minutes is just right"),
    ("owner@example.com", "continue", False, "Continue giving us room to pick our own tasks"),
    ("member4@example.com", "continue", True, "Continue the friendly tone in code review comments"),
)


class SeedError(Exception):
    """A refusal or invalid input; the message is shown to the person on stderr."""


def _foreign_users(engine: Engine) -> list[str]:
    """Emails in the users table that are not seed emails (empty if there is no users table)."""
    if "users" not in inspect(engine).get_table_names():
        return []
    with Session(engine) as session:
        return [e for e in session.scalars(select(User.email)) if e not in SEED_EMAILS]


def _refuse_if_foreign_users(engine: Engine) -> None:
    if _foreign_users(engine):
        raise SeedError(
            "Refusing to seed: the database already contains users other than the demo users. "
            "Nothing was changed."
        )


def _timestamps(schedule: Schedule, week: str, count: int) -> list[datetime]:
    """Deterministic naive-UTC stamps spread evenly inside the week's window (so listing order is stable)."""
    opens, closes = _window(schedule, _parse_week(week, schedule))
    span = closes - opens
    stamps = []
    for i in range(count):
        moment = (opens + span * (i + 1) / (count + 1)).astimezone(UTC)
        stamps.append(moment.replace(microsecond=0, tzinfo=None))
    return stamps


def seed(week: str, db_path: str, schedule: Schedule, *, engine: Engine | None = None) -> dict:
    """Seed `db_path` for `week` (already validated). Returns counts of what was created."""
    own_engine = engine is None
    engine = engine or make_engine(db_path)
    try:
        created_users = 0
        for email in SEED_EMAILS:
            with Session(engine) as session:
                exists = session.scalar(select(User.id).where(User.email == normalise_email(email)))
            if exists is None:
                create_user(
                    email,
                    DEMO_PASSWORD,
                    "owner" if email == OWNER_EMAIL else "member",
                    display_name=SEED_NAMES[email],
                    engine=engine,
                )
                created_users += 1

        with Session(engine) as session:
            created_project = session.get(Project, 1) is None
            if created_project:
                session.add(Project(id=1, name=PROJECT_NAME))

            ids = dict(session.execute(select(User.email, User.id).where(User.email.in_(SEED_EMAILS))).all())
            already = session.scalar(
                select(FeedbackItem.id)
                .where(FeedbackItem.week == week, FeedbackItem.author_id.in_(ids.values()))
                .limit(1)
            )
            created_items = 0
            if already is None:
                for (email, category, anonymous, text), stamp in zip(
                    SAMPLE_ITEMS, _timestamps(schedule, week, len(SAMPLE_ITEMS))
                ):
                    session.add(
                        FeedbackItem(
                            author_id=ids[email],
                            week=week,
                            category=category,
                            text=text,
                            anonymous=anonymous,
                            created_at=stamp,
                            updated_at=stamp,
                        )
                    )
                    created_items += 1
            session.commit()
        return {"users": created_users, "project": int(created_project), "items": created_items}
    finally:
        if own_engine:
            engine.dispose()


def run(week_arg: str | None) -> int:
    # 1. production check: before anything is loaded or touched
    if os.environ.get("WEEKLY_ENV") == "production":
        raise SeedError("Refusing to seed: WEEKLY_ENV is production. Nothing was changed.")
    try:
        settings = load_settings()
    except ConfigError as error:
        raise SeedError(f"Invalid configuration: {error}") from None
    schedule = Schedule.from_settings(settings)

    # 2. validate the week before touching the database
    if week_arg is None:
        week = describe(datetime.now(UTC), schedule).week_id
    else:
        try:
            week = _parse_week(week_arg, schedule)
        except ValueError as error:
            raise SeedError(f"Invalid --week {week_arg!r}: {error}") from None
        week = week.isoformat()

    # 3. real-user check without migrating when the database already has a users table
    db_path = settings.db_path
    if Path(db_path).exists():
        engine = make_engine(db_path)
        try:
            _refuse_if_foreign_users(engine)
        finally:
            engine.dispose()

    # 4. migrate (schema only), then check again (a database that was behind has no users table before)
    migrate(db_path)
    engine = make_engine(db_path)
    try:
        _refuse_if_foreign_users(engine)
        counts = seed(week, db_path, schedule, engine=engine)
    finally:
        engine.dispose()

    print(f"Seeded week {week}: {counts['users']} new users, {counts['project']} new project, {counts['items']} new items.")
    print(f"Owner: {OWNER_EMAIL}")
    print(f"Password (all demo users): {DEMO_PASSWORD}")
    print(f"Week: {week}")
    print("WARNING: the demo password is public. Use this database for local demos only.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seed", description="Seed a local demo team and feedback.")
    parser.add_argument("--week", help="week to fill, YYYY-MM-DD, the date its window opens (default: current week)")
    args = parser.parse_args(argv)
    try:
        return run(args.week)
    except SeedError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
