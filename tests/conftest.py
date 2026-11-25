import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.db import make_engine, migrate


@contextmanager
def isolated_db(monkeypatch):
    """A fresh, migrated, empty SQLite database in its own temp directory.

    WEEKLY_DB_PATH points at it for the duration; the directory is removed on
    exit, so nothing is left behind or shared.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="weekly-test-"))
    monkeypatch.setenv("WEEKLY_DB_PATH", str(tmpdir / "test.db"))
    migrate()
    engine = make_engine()
    try:
        yield engine
    finally:
        engine.dispose()
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def db_engine(monkeypatch):
    """Per-test isolated, migrated database engine (foreign keys on)."""
    with isolated_db(monkeypatch) as engine:
        yield engine
