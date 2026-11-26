"""SQLAlchemy models. Timestamps are stored in UTC (SQLite CURRENT_TIMESTAMP)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, MetaData, String, Text, UniqueConstraint, func, text as sql_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _now():
    return mapped_column(DateTime, server_default=func.current_timestamp())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('owner', 'member')", name="role"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)  # stored already normalised (#4)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = _now()
    display_name: Mapped[str] = mapped_column(String)  # chosen at sign-up (#29); no default, see app.users


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, server_default=sql_text("1"))
    name: Mapped[str] = mapped_column(String)


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String, unique=True)  # never the plain token
    invited_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _now()
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class FeedbackItem(Base):
    __tablename__ = "feedback_items"
    __table_args__ = (
        CheckConstraint("category IN ('start', 'stop', 'continue')", name="category"),
        CheckConstraint("anonymous IN (0, 1)", name="anonymous"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    week: Mapped[str] = mapped_column(String)  # week identifier, defined in #10
    category: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    anonymous: Mapped[bool] = mapped_column(server_default=sql_text("0"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class ReminderSent(Base):
    """One row per reminder email sent: a member gets at most one per week (#19)."""

    __tablename__ = "reminders_sent"
    __table_args__ = (UniqueConstraint("user_id", "week", name="uq_reminders_sent_user_id_week"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    week: Mapped[str] = mapped_column(String)
    sent_at: Mapped[datetime] = _now()
