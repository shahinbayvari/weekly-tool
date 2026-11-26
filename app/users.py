"""Create and look up users. Service layer only; no web routes."""

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import User
from app.passwords import hash_password

ROLES = ("owner", "member")


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


def create_user(email: str, password: str, role: str, *, engine: Engine | None = None) -> User:
    """Create a user. Everything is validated before anything is written.

    Uses `engine` if given, otherwise the engine for WEEKLY_DB_PATH.
    """
    normalised = normalise_email(email)
    validate_email(normalised)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    password_hash = hash_password(password)  # raises WeakPasswordError

    own_engine = engine is None
    engine = engine or make_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            user = User(email=normalised, password_hash=password_hash, role=role)
            session.add(user)
            try:
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
