"""The submission window: every write needs an open window at a fixed `now`."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.config import Settings
from app.items import (
    ItemNotFoundError,
    NotAuthorError,
    WindowClosedError,
    add_item,
    delete_item,
    edit_item,
    list_items,
    set_anonymous,
)
from app.users import create_user

AMS = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
US = timedelta(microseconds=1)

OPEN = datetime(2026, 1, 5, 9, 0, tzinfo=AMS)  # Monday: opening instant, week 2026-01-05
DEADLINE = datetime(2026, 1, 9, 17, 0, tzinfo=AMS)  # Friday: closing instant
MID = datetime(2026, 1, 7, 12, 0, tzinfo=AMS)
WEEK = "2026-01-05"
NEXT_OPEN = datetime(2026, 1, 12, 9, 0, tzinfo=AMS)


@pytest.fixture
def users(db_engine):
    ann = create_user("ann@example.com", "correct horse", "member", engine=db_engine)
    owner = create_user("own@example.com", "correct horse", "owner", engine=db_engine)
    return ann, owner


def snapshot(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM feedback_items ORDER BY id")).all()


def kw(engine):
    return {"engine": engine, "settings": SETTINGS}


def seed(engine, user):
    return add_item(user.id, MID, "start", "orig", **kw(engine))


def run(op, engine, item, user, now):
    """Run one of the four writes by name."""
    if op == "add":
        return add_item(user.id, now, "start", "new", **kw(engine))
    if op == "edit":
        return edit_item(item.id, user.id, now, "changed", **kw(engine))
    if op == "delete":
        return delete_item(item.id, user.id, now, **kw(engine))
    return set_anonymous(item.id, user.id, now, True, **kw(engine))


OPS = ["add", "edit", "delete", "anonymous"]


@pytest.mark.parametrize("op", OPS)
def test_open_window_works(db_engine, users, op):
    ann, _ = users
    item = seed(db_engine, ann)
    run(op, db_engine, item, ann, MID)
    assert snapshot(db_engine) != [] or op == "delete"


def test_add_files_under_current_week(db_engine, users):
    ann, _ = users
    assert add_item(ann.id, MID, "start", "a", **kw(db_engine)).week == WEEK
    assert add_item(ann.id, NEXT_OPEN, "stop", "b", **kw(db_engine)).week == "2026-01-12"


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("now,label", [(DEADLINE - US, "before deadline"), (OPEN, "opening instant")])
def test_allowed_boundaries(db_engine, users, op, now, label):
    ann, _ = users
    item = seed(db_engine, ann)
    run(op, db_engine, item, ann, now)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(
    "now",
    [DEADLINE, DEADLINE + US, datetime(2026, 1, 11, 12, 0, tzinfo=AMS), NEXT_OPEN - US, datetime(2026, 1, 5, 9, 0, tzinfo=AMS) - US],
)
def test_refused_when_closed_and_nothing_changes(db_engine, users, op, now):
    ann, _ = users
    item = seed(db_engine, ann)
    before = snapshot(db_engine)
    with pytest.raises(WindowClosedError):
        run(op, db_engine, item, ann, now)
    assert snapshot(db_engine) == before


@pytest.mark.parametrize("op", OPS)
def test_owner_has_no_exemption(db_engine, users, op):
    _, owner = users
    item = seed(db_engine, owner)
    before = snapshot(db_engine)
    with pytest.raises(WindowClosedError):
        run(op, db_engine, item, owner, DEADLINE)
    assert snapshot(db_engine) == before


@pytest.mark.parametrize("op", ["edit", "delete", "anonymous"])
def test_past_week_item_refused_while_new_window_open(db_engine, users, op):
    ann, owner = users
    for user in (ann, owner):
        item = seed(db_engine, user)
        before = snapshot(db_engine)
        with pytest.raises(WindowClosedError):
            run(op, db_engine, item, user, NEXT_OPEN)
        assert snapshot(db_engine) == before


def test_past_week_item_refused_at_new_window_opening_only_for_old_items(db_engine, users):
    ann, _ = users
    old = seed(db_engine, ann)
    new = add_item(ann.id, NEXT_OPEN, "start", "fresh", **kw(db_engine))
    assert edit_item(new.id, ann.id, NEXT_OPEN, "ok", **kw(db_engine)).text == "ok"
    with pytest.raises(WindowClosedError):
        edit_item(old.id, ann.id, NEXT_OPEN, "no", **kw(db_engine))


def test_message_states_closed_and_next_opening_in_configured_zone(db_engine, users):
    ann, _ = users
    with pytest.raises(WindowClosedError) as exc:
        add_item(ann.id, DEADLINE.astimezone(timezone.utc), "start", "x", **kw(db_engine))
    msg = str(exc.value)
    assert "closed" in msg
    assert "2026-01-12 09:00" in msg  # Monday 09:00 Amsterdam, not UTC 08:00
    assert "Monday" in msg
    assert "Europe/Amsterdam" in msg


@pytest.mark.parametrize("op", OPS)
def test_naive_now_raises_value_error_before_anything_else(db_engine, users, op):
    ann, _ = users
    item = seed(db_engine, ann)
    before = snapshot(db_engine)
    naive = datetime(2026, 1, 7, 12, 0)
    with pytest.raises(ValueError) as exc:
        run(op, db_engine, item, ann, naive)
    assert not isinstance(exc.value, WindowClosedError)
    assert snapshot(db_engine) == before


def test_naive_now_beats_invalid_input_and_unknown_item(db_engine, users):
    ann, _ = users
    naive = datetime(2026, 1, 7, 12, 0)
    with pytest.raises(ValueError, match="timezone"):
        add_item(ann.id, naive, "bogus", "", **kw(db_engine))
    with pytest.raises(ValueError, match="timezone"):
        edit_item(999, ann.id, naive, "", **kw(db_engine))
    with pytest.raises(ValueError, match="timezone"):
        delete_item(999, ann.id, naive, **kw(db_engine))


def test_closed_is_checked_before_other_errors(db_engine, users):
    ann, owner = users
    item = seed(db_engine, ann)
    with pytest.raises(WindowClosedError):
        add_item(ann.id, DEADLINE, "bogus", "", **kw(db_engine))
    with pytest.raises(WindowClosedError):
        edit_item(999, ann.id, DEADLINE, "x", **kw(db_engine))
    with pytest.raises(WindowClosedError):
        delete_item(item.id, owner.id, DEADLINE, **kw(db_engine))


def test_open_window_keeps_other_errors(db_engine, users):
    ann, owner = users
    item = seed(db_engine, ann)
    with pytest.raises(ItemNotFoundError):
        edit_item(999, ann.id, MID, "x", **kw(db_engine))
    with pytest.raises(NotAuthorError):
        delete_item(item.id, owner.id, MID, **kw(db_engine))


def test_list_items_works_when_closed(db_engine, users):
    ann, _ = users
    item = seed(db_engine, ann)
    assert [i.id for i in list_items(ann.id, WEEK, engine=db_engine)["start"]] == [item.id]


def test_settings_default_to_environment(db_engine, users, monkeypatch):
    ann, _ = users
    monkeypatch.setenv("WEEKLY_TIMEZONE", "Europe/Amsterdam")
    assert add_item(ann.id, OPEN, "start", "x", engine=db_engine).week == WEEK
    with pytest.raises(WindowClosedError):
        add_item(ann.id, DEADLINE, "start", "x", engine=db_engine)
