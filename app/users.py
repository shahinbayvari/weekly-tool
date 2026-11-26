"""Create and look up users. Service layer only; no web routes."""

import unicodedata

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import User
from app.passwords import hash_password

ROLES = ("owner", "member")

MAX_DISPLAY_NAME_LENGTH = 50
RESERVED_NAME = "anonymous"  # the reveal page labels anonymous items with this word


class InvalidEmailError(ValueError):
    """The email is empty or has no '@'."""


class DuplicateEmailError(ValueError):
    """A user with this (normalised) email already exists."""


def normalise_email(email: str) -> str:
    return email.strip().lower()


def validate_email(normalised: str) -> None:
    """Raise InvalidEmailError unless the (already normalised) email is non-empty and has an at-sign."""
    if not normalised or "@" not in normalised:
        raise InvalidEmailError("Invalid email address")


class InvalidDisplayNameError(ValueError):
    """The display name breaks a rule; the message is shown to the person."""


def validate_display_name(raw: str) -> str:
    """Return the trimmed display name or raise InvalidDisplayNameError (one rule set for every form).

    Rules, in order: trim; empty, or only invisible characters (format characters such as
    zero-width space, and spaces), is "Enter a name"; any control character (Unicode category Cc:
    tab, newline, 0x00-0x1f, 0x7f-0x9f including U+0085) or line/paragraph separator (U+2028,
    U+2029) is "Name must be a single line"; more than 50 characters (len) is too long;
    "anonymous" in any letter case (ignoring invisible format characters) is reserved.
    Inner spaces and emoji / non-Latin letters are kept as typed.
    """
    name = raw.strip()
    if all(unicodedata.category(c) in ("Cf", "Zs") for c in name):
        raise InvalidDisplayNameError("Enter a name")
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name):
        raise InvalidDisplayNameError("Name must be a single line")
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayNameError(f"Name must be at most {MAX_DISPLAY_NAME_LENGTH} characters")
    visible = "".join(c for c in name if unicodedata.category(c) != "Cf")
    if visible.strip().casefold() == RESERVED_NAME:
        raise InvalidDisplayNameError("Choose a different name")
    return name


def default_display_name(email: str, user_id: int) -> str:
    """The name a user gets when none is given: the email's part before the "@", trimmed and cut to 50
    characters; `User <id>` if that is empty or is "anonymous" in any letter case.

    The migration that added display_name (0003) uses this same function for existing users.
    """
    name = email.partition("@")[0].strip()[:MAX_DISPLAY_NAME_LENGTH].strip()
    if not name or name.casefold() == RESERVED_NAME:
        return f"User {user_id}"
    return name


def create_user(
    email: str, password: str, role: str, *, display_name: str | None = None, engine: Engine | None = None
) -> User:
    """Create a user. Everything is validated before anything is written.

    Uses `engine` if given, otherwise the engine for WEEKLY_DB_PATH.
    """
    normalised = normalise_email(email)
    validate_email(normalised)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    password_hash = hash_password(password)  # raises WeakPasswordError
    if display_name is not None:
        display_name = validate_display_name(display_name)  # raises InvalidDisplayNameError

    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            user = User(email=normalised, password_hash=password_hash, role=role, display_name=display_name or "")
            session.add(user)
            try:
                if display_name is None:
                    session.flush()  # the id is needed for the "User <id>" fallback
                    user.display_name = default_display_name(normalised, user.id)
                session.commit()
            except IntegrityError:
                session.rollback()
                raise DuplicateEmailError("A user with this email already exists") from None
            return user
    finally:
        if own_engine:
            engine.dispose()


def find_user_by_email(email: str, *, engine: Engine | None = None) -> User | None:
    """The user with this email (trimmed, case-insensitive), or None. Never raises for bad input."""
    normalised = normalise_email(email)
    if not normalised:
        return None
    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            return session.scalar(select(User).where(User.email == normalised))
    finally:
        if own_engine:
            engine.dispose()


def update_password_hash_if_unchanged(user_id: int, old_hash: str, new_hash: str, *, engine: Engine) -> bool:
    """Compare-and-set: store `new_hash` only if the stored hash is still `old_hash`.

    Returns True if a row was updated, False if the hash had changed meanwhile (nothing written).
    """
    with engine.begin() as conn:
        result = conn.execute(
            update(User).where(User.id == user_id, User.password_hash == old_hash).values(password_hash=new_hash)
        )
    return result.rowcount == 1
