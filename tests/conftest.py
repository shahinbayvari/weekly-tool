import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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


# The reminder scheduler must never start during tests. Set before any test
# module imports app.main (which reads the settings at import time).
os.environ["WEEKLY_SCHEDULER_ENABLED"] = "false"


# --- CSRF test helper ---------------------------------------------------------
# One shared helper so existing tests send the token like a browser does: before a
# state-changing request it loads a page, reads the token from the page's hidden
# field (or the <body hx-headers> attribute) and sends it as the `csrf_token` form
# field. Pass `csrf=False` to send the request exactly as given (no token), and
# `csrf_token="..."` to send a specific value.
import re  # noqa: E402

_TOKEN_RES = (
    re.compile(r'name="csrf_token" value="([^"]*)"'),
    re.compile(r'X-CSRF-Token": "([^"]*)"'),
)
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def strip_csrf(html: str) -> str:
    """HTML without the per-session CSRF hidden fields (for tests that compare or scan page content)."""
    return re.sub(r'<input type="hidden" name="csrf_token" value="[^"]*">', "", html)


class CsrfClient(TestClient):
    def csrf_token(self) -> str:
        """The current session's token, read from a page (/login signed out, else /submit)."""
        for path in ("/login", "/setup", "/submit"):
            r = super().request("GET", path)
            if r.status_code == 200:
                for rx in _TOKEN_RES:
                    m = rx.search(r.text)
                    if m:
                        return m.group(1)
        raise AssertionError("could not find a CSRF token on /login, /setup or /submit")

    def request(self, method, url, *args, csrf=True, csrf_token=None, **kwargs):
        if method.upper() in UNSAFE and csrf:
            if csrf_token is None:
                csrf_token = self.csrf_token()
            data = kwargs.get("data")
            if data is None:
                kwargs["data"] = {"csrf_token": csrf_token}
            elif isinstance(data, dict):
                kwargs["data"] = {"csrf_token": csrf_token, **data}
        return super().request(method, url, *args, **kwargs)

    # httpx's post/put/patch/delete do not forward unknown keywords to request().
    def post(self, url, *args, csrf=True, csrf_token=None, **kwargs):
        return self.request("POST", url, *args, csrf=csrf, csrf_token=csrf_token, **kwargs)

    def put(self, url, *args, csrf=True, csrf_token=None, **kwargs):
        return self.request("PUT", url, *args, csrf=csrf, csrf_token=csrf_token, **kwargs)

    def patch(self, url, *args, csrf=True, csrf_token=None, **kwargs):
        return self.request("PATCH", url, *args, csrf=csrf, csrf_token=csrf_token, **kwargs)

    def delete(self, url, *args, csrf=True, csrf_token=None, **kwargs):
        return self.request("DELETE", url, *args, csrf=csrf, csrf_token=csrf_token, **kwargs)


@pytest.fixture
def csrf_client_factory():
    """Build CsrfClient(app) instances; closes none itself (use as a context manager)."""
    return CsrfClient
