"""Database engine and migrations.

The database file is `WEEKLY_DB_PATH` (see app.config). Apply all migrations
(safe to run repeatedly; a second run changes nothing) with either:

    uv run python -m alembic upgrade head
    uv run python -c "from app.db import migrate; migrate()"
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event

from app.config import load_settings

ROOT = Path(__file__).resolve().parent.parent


def make_engine(db_path: str | None = None) -> Engine:
    """Engine for `db_path` (default: WEEKLY_DB_PATH) with foreign keys on per connection."""
    if db_path is None:
        db_path = load_settings().db_path
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _foreign_keys_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def migrate(db_path: str | None = None) -> None:
    """Apply all migrations to `db_path` (default: WEEKLY_DB_PATH), creating the file if needed."""
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    if db_path is not None:
        cfg.attributes["db_path"] = db_path
    command.upgrade(cfg, "head")
