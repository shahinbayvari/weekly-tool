import json
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from app.config import Settings
from app.cycle import Schedule
from app.items import add_item
from app.reveal import AnonymousItem, NamedItem, reveal_week
from app.users import create_user

TZ = ZoneInfo("Europe/Amsterdam")
SETTINGS = Settings(timezone="Europe/Amsterdam", open_day="monday", open_time="09:00", close_day="friday", close_time="17:00")
WEEK = "2026-01-05"
OPEN_NOW = datetime(2026, 1, 7, 12, 0, tzinfo=TZ)
DEADLINE = datetime(2026, 1, 9, 17, 0, tzinfo=TZ)
AFTER = datetime(2026, 1, 10, 12, 0, tzinfo=TZ)


@pytest.fixture
def users(db_engine):
    ann = create_user("ann@example.com", "correct horse", "member", engine=db_engine)
    bob = create_user("bob@example.com", "correct horse", "member", engine=db_engine)
    return ann, bob


def add(engine, user, category, text, anonymous=False, now=OPEN_NOW):
    return add_item(user.id, now, category, text, anonymous, engine=engine, settings=SETTINGS)


def reveal(engine, week=WEEK, now=AFTER):
    return reveal_week(week, now, engine=engine, settings=SETTINGS)


def statements(engine):
    log = []

    def capture(conn, cursor, statement, *args):
        log.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    return log


def test_open_window_not_revealed_and_no_query(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "start", "x")
    log = statements(db_engine)
    r = reveal(db_engine, now=OPEN_NOW)
    assert (r.revealed, r.start, r.stop, r.continue_) == (False, (), (), ())
    assert log == []


def test_deadline_boundary(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "start", "x")
    assert reveal(db_engine, now=DEADLINE - timedelta(microseconds=1)).revealed is False
    r = reveal(db_engine, now=DEADLINE)
    assert r.revealed is True and len(r.start) == 1


def test_future_week_not_revealed_no_query(db_engine):
    log = statements(db_engine)
    for week in ("2026-01-12", "2030-01-07"):
        r = reveal(db_engine, week=week, now=AFTER)
        assert r.revealed is False and r.start == r.stop == r.continue_ == ()
    assert log == []


def test_older_week_revealed(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "stop", "old")
    r = reveal(db_engine, now=datetime(2027, 1, 1, tzinfo=TZ))
    assert r.revealed and [i.text for i in r.stop] == ["old"]


@pytest.mark.parametrize(
    "week", ["2026-13-01", "2026-1-1", "abc", "", "2026-02-30", "20260105", " 2026-01-05", "2026-01-05\n"]
)
def test_invalid_week(db_engine, week):
    with pytest.raises(ValueError):
        reveal(db_engine, week=week)


def test_non_open_day_rejected(db_engine):
    with pytest.raises(ValueError):
        reveal(db_engine, week="2026-01-06")  # Tuesday, but the window opens on Monday


def test_naive_now_rejected(db_engine):
    with pytest.raises(ValueError):
        reveal(db_engine, now=datetime(2026, 1, 10, 12, 0))


def test_schedule_accepted_and_deadline_across_dst(db_engine):
    sched = Schedule.from_settings(SETTINGS)
    # DST began Sunday 2026-03-29: week 03-23 closes at 17:00 CET, week 03-30 at 17:00 CEST.
    for week, deadline in (("2026-03-23", datetime(2026, 3, 27, 17, 0, tzinfo=TZ)), ("2026-03-30", datetime(2026, 4, 3, 17, 0, tzinfo=TZ))):
        assert reveal_week(week, deadline - timedelta(microseconds=1), engine=db_engine, settings=sched).revealed is False
        assert reveal_week(week, deadline, engine=db_engine, settings=sched).revealed is True


def test_empty_week_revealed(db_engine):
    r = reveal(db_engine)
    assert r.revealed is True and (r.start, r.stop, r.continue_) == ((), (), ())


def test_grouping_and_item_shapes(db_engine, users):
    ann, bob = users
    add(db_engine, ann, "start", "s1")
    add(db_engine, bob, "stop", "t1", anonymous=True)
    add(db_engine, bob, "continue", "c1")
    r = reveal(db_engine)
    assert r.start == (NamedItem("s1", "start", "ann@example.com"),)
    assert r.stop == (AnonymousItem("t1", "stop"),)
    assert r.continue_ == (NamedItem("c1", "continue", "bob@example.com"),)
    assert r.by_category("continue") == r.continue_
    assert set(asdict(r.stop[0])) == {"text", "category"}


def test_other_weeks_excluded(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "start", "this week")
    add(db_engine, ann, "start", "next week", now=OPEN_NOW + timedelta(days=7))
    r = reveal(db_engine, now=datetime(2027, 1, 1, tzinfo=TZ))
    assert [i.text for i in r.start] == ["this week"]


def test_anonymous_author_not_in_serialisation(db_engine, users):
    ann, bob = users
    anon1 = add(db_engine, ann, "start", "secret one", anonymous=True)
    anon2 = add(db_engine, ann, "stop", "secret two", anonymous=True)
    add(db_engine, bob, "start", "public")
    r = reveal(db_engine)
    blobs = [repr(r), str(r), json.dumps(asdict(r)), json.dumps(asdict(r), default=str)]
    for blob in blobs:
        assert "ann@example.com" not in blob
        assert "ann" not in blob
        assert "bob@example.com" in blob
    # The author's id and the item ids are not present as fields or values on anonymous items.
    anon_items = [i for i in (*r.start, *r.stop) if isinstance(i, AnonymousItem)]
    assert len(anon_items) == 2
    for item in anon_items:
        assert set(asdict(item)) == {"text", "category"}
        assert ann.id not in asdict(item).values() and anon1.id not in asdict(item).values() and anon2.id not in asdict(item).values()


def test_mixed_author_email_only_on_named(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "start", "named", anonymous=False)
    add(db_engine, ann, "start", "hidden", anonymous=True)
    r = reveal(db_engine)
    by_text = {i.text: i for i in r.start}
    assert by_text["named"].email == "ann@example.com"
    assert not hasattr(by_text["hidden"], "email")
    assert "ann@example.com" not in repr(by_text["hidden"])


def test_ordering_casefold_then_exact(db_engine, users):
    ann, _ = users
    for text in ["banana", "Apple", "apple", "Banana", "cherry"]:
        add(db_engine, ann, "start", text)
    assert [i.text for i in reveal(db_engine).start] == ["Apple", "apple", "Banana", "banana", "cherry"]


def test_ordering_ignores_author_and_insertion_order(db_engine, users):
    ann, bob = users
    add(db_engine, ann, "start", "b named")
    add(db_engine, ann, "start", "a anon", anonymous=True)
    add(db_engine, bob, "start", "c bob")
    add(db_engine, ann, "start", "d anon", anonymous=True)
    add(db_engine, ann, "start", "e named")
    r = reveal(db_engine)
    assert [i.text for i in r.start] == ["a anon", "b named", "c bob", "d anon", "e named"]
    assert [type(i).__name__ for i in r.start] == ["AnonymousItem", "NamedItem", "NamedItem", "AnonymousItem", "NamedItem"]


def test_same_text_named_and_anonymous_deterministic(db_engine, users):
    ann, bob = users
    add(db_engine, bob, "start", "same")
    add(db_engine, ann, "start", "same", anonymous=True)
    add(db_engine, ann, "start", "same")
    assert reveal(db_engine).start == (
        AnonymousItem("same", "start"),
        NamedItem("same", "start", "ann@example.com"),
        NamedItem("same", "start", "bob@example.com"),
    )


def test_same_text_result_independent_of_insertion_order(monkeypatch, users, db_engine):
    ann, bob = users
    add(db_engine, ann, "start", "same")
    add(db_engine, bob, "start", "same", anonymous=True)
    add(db_engine, bob, "start", "same")
    first = reveal(db_engine).start
    # Same entries inserted in the opposite order give the same result.
    from tests.conftest import isolated_db

    with isolated_db(monkeypatch) as engine2:
        ann2 = create_user("ann@example.com", "correct horse", "member", engine=engine2)
        bob2 = create_user("bob@example.com", "correct horse", "member", engine=engine2)
        add(engine2, bob2, "start", "same")
        add(engine2, bob2, "start", "same", anonymous=True)
        add(engine2, ann2, "start", "same")
        assert reveal(engine2).start == first


def test_repeated_calls_identical(db_engine, users):
    ann, bob = users
    add(db_engine, ann, "start", "one", anonymous=True)
    add(db_engine, bob, "stop", "two")
    first = reveal(db_engine)
    assert reveal(db_engine) == first == reveal(db_engine, now=DEADLINE)


def test_anonymous_query_selects_only_text_and_category(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "start", "anon", anonymous=True)
    log = statements(db_engine)
    reveal(db_engine)
    selects = [s for s in log if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2
    named = [s for s in selects if "users" in s]
    anon = [s for s in selects if "users" not in s]
    assert len(named) == 1 and len(anon) == 1
    columns = anon[0].split("FROM")[0]
    assert "feedback_items.text" in columns and "feedback_items.category" in columns
    for forbidden in ("author_id", "feedback_items.id", "created_at", "updated_at", "email", "anonymous"):
        assert forbidden not in columns
    assert "JOIN" not in anon[0].upper()
    # The named query only ever covers non-anonymous rows.
    assert "anonymous = 0" in named[0] or "anonymous IS 0" in named[0]


def test_default_engine(db_engine, users):
    ann, _ = users
    add(db_engine, ann, "start", "x")
    assert len(reveal_week(WEEK, AFTER, settings=SETTINGS).start) == 1
