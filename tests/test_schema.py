import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import make_engine, migrate
from app.models import Base

TABLES = {"users", "project", "invites", "feedback_items"}
ROOT = Path(__file__).resolve().parent.parent


def _ddl(engine):
    """Normalised CREATE statements of every user table, keyed by table name."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type='table' AND name != 'alembic_version'")
        ).all()
    return {name: re.sub(r"[\s\"]+", "", sql) for name, sql in rows}


def _user(conn, email="a@example.com", role="member"):
    return conn.execute(
        text("INSERT INTO users (email, password_hash, role) VALUES (:e, 'h', :r)"),
        {"e": email, "r": role},
    ).lastrowid


def _item(conn, author, category="start"):
    conn.execute(
        text("INSERT INTO feedback_items (author_id, week, category, text) VALUES (:a, '2026-W01', :c, 'x')"),
        {"a": author, "c": category},
    )


# --- tables and migrations ---------------------------------------------------


def test_migrations_create_all_four_tables(db_engine):
    assert TABLES <= set(inspect(db_engine).get_table_names())


def test_migrate_twice_changes_nothing(db_engine):
    before = _ddl(db_engine)
    with db_engine.begin() as conn:
        conn.execute(text("INSERT INTO project (name) VALUES ('keep me')"))
    migrate()
    assert _ddl(db_engine) == before
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT name FROM project")).scalar_one() == "keep me"
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1


def test_migrations_match_models_no_autogenerate_diff(db_engine):
    with db_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_migrated_ddl_matches_models_ddl(db_engine):
    """Also covers CHECK constraints, which autogenerate does not compare."""
    tmpdir = tempfile.mkdtemp(prefix="weekly-models-")
    try:
        fresh = create_engine(f"sqlite:///{Path(tmpdir) / 'models.db'}")
        Base.metadata.create_all(fresh)
        expected = _ddl(fresh)
        fresh.dispose()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    assert _ddl(db_engine) == expected


def test_alembic_command_line_applies_to_weekly_db_path():
    tmpdir = tempfile.mkdtemp(prefix="weekly-cli-")
    try:
        db = Path(tmpdir) / "cli.db"
        env = {**os.environ, "WEEKLY_DB_PATH": str(db)}
        for _ in range(2):  # second run must be a no-op
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=ROOT, env=env, check=True, capture_output=True,
            )
        engine = create_engine(f"sqlite:///{db}")
        assert TABLES <= set(inspect(engine).get_table_names())
        engine.dispose()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- fixture isolation -------------------------------------------------------


def test_fixture_db_path_comes_from_weekly_db_path(db_engine):
    assert Path(db_engine.url.database) == Path(os.environ["WEEKLY_DB_PATH"])


def test_fixture_isolation_write(db_engine):
    with db_engine.begin() as conn:
        _user(conn, "leak@example.com")


def test_fixture_isolation_read(db_engine):
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0


def test_fixture_leaves_no_file_behind(monkeypatch):
    from tests.conftest import isolated_db

    with isolated_db(monkeypatch) as engine:
        path = Path(engine.url.database)
        assert path.exists()
    assert not path.exists()
    assert not path.parent.exists()


# --- constraints -------------------------------------------------------------


def test_foreign_keys_pragma_on_every_connection(db_engine):
    for _ in range(2):
        with db_engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_item_with_unknown_author_fails(db_engine):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with db_engine.begin() as conn:
            _item(conn, author=999)


def test_invite_with_unknown_inviter_fails(db_engine):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with db_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO invites (email, token_hash, invited_by) VALUES ('b@example.com', 't', 999)")
            )


def test_item_with_known_author_ok(db_engine):
    with db_engine.begin() as conn:
        _item(conn, _user(conn))
        row = conn.execute(text("SELECT anonymous, created_at, updated_at FROM feedback_items")).one()
    assert row.anonymous == 0 and row.created_at and row.updated_at


def test_duplicate_email_fails(db_engine):
    with pytest.raises(IntegrityError, match="UNIQUE"):
        with db_engine.begin() as conn:
            _user(conn, "dup@example.com")
            _user(conn, "dup@example.com", role="owner")


def test_second_project_row_fails(db_engine):
    with db_engine.begin() as conn:
        conn.execute(text("INSERT INTO project (name) VALUES ('one')"))
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(text("INSERT INTO project (name) VALUES ('two')"))
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(text("INSERT INTO project (id, name) VALUES (2, 'two')"))


def test_role_admin_fails(db_engine):
    with pytest.raises(IntegrityError, match="CHECK"):
        with db_engine.begin() as conn:
            _user(conn, role="admin")


@pytest.mark.parametrize("role", ["owner", "member"])
def test_valid_roles_ok(db_engine, role):
    with db_engine.begin() as conn:
        _user(conn, role=role)


def test_category_idea_fails(db_engine):
    with db_engine.begin() as conn:
        author = _user(conn)
    with pytest.raises(IntegrityError, match="CHECK"):
        with db_engine.begin() as conn:
            _item(conn, author, category="idea")


@pytest.mark.parametrize("category", ["start", "stop", "continue"])
def test_valid_categories_ok(db_engine, category):
    with db_engine.begin() as conn:
        _item(conn, _user(conn), category=category)


def test_duplicate_invite_token_hash_fails(db_engine):
    with db_engine.begin() as conn:
        uid = _user(conn)
        conn.execute(text("INSERT INTO invites (email, token_hash, invited_by) VALUES ('x@e.com', 't', :u)"), {"u": uid})
    with pytest.raises(IntegrityError, match="UNIQUE"):
        with db_engine.begin() as conn:
            conn.execute(text("INSERT INTO invites (email, token_hash, invited_by) VALUES ('y@e.com', 't', :u)"), {"u": uid})


def test_invite_timestamps_default_empty(db_engine):
    with db_engine.begin() as conn:
        uid = _user(conn)
        conn.execute(text("INSERT INTO invites (email, token_hash, invited_by) VALUES ('x@e.com', 't', :u)"), {"u": uid})
        row = conn.execute(text("SELECT accepted_at, revoked_at, created_at FROM invites")).one()
    assert row.accepted_at is None and row.revoked_at is None and row.created_at


@pytest.mark.parametrize("column", ["author_id", "week", "category", "text"])
def test_item_required_columns(db_engine, column):
    with db_engine.begin() as conn:
        author = _user(conn)
    values = {"author_id": author, "week": "2026-W01", "category": "start", "text": "x"}
    values[column] = None
    with pytest.raises(IntegrityError, match="NOT NULL"):
        with db_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO feedback_items (author_id, week, category, text) VALUES (:author_id, :week, :category, :text)"),
                values,
            )


def test_make_engine_defaults_to_weekly_db_path(monkeypatch):
    monkeypatch.setenv("WEEKLY_DB_PATH", "somewhere.db")
    engine = make_engine()
    assert engine.url.database == "somewhere.db"
    engine.dispose()
