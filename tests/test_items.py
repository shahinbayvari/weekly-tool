import pytest
from sqlalchemy import text

from app.items import (
    CATEGORIES,
    MAX_TEXT_LENGTH,
    InvalidTextError,
    ItemNotFoundError,
    NotAuthorError,
    UnknownUserError,
    add_item,
    delete_item,
    edit_item,
    list_items,
    set_anonymous,
)
from app.users import create_user

WEEK = "2026-01-05"


@pytest.fixture
def users(db_engine):
    ann = create_user("ann@example.com", "correct horse", "member", engine=db_engine)
    bob = create_user("bob@example.com", "correct horse", "member", engine=db_engine)
    owner = create_user("own@example.com", "correct horse", "owner", engine=db_engine)
    return ann, bob, owner


def rows(engine):
    with engine.connect() as c:
        return c.execute(
            text("SELECT id, author_id, week, category, text, anonymous, created_at, updated_at FROM feedback_items ORDER BY id")
        ).all()


def test_constants():
    assert MAX_TEXT_LENGTH == 280
    assert CATEGORIES == ("start", "stop", "continue")


def test_add_returns_and_stores_item(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "start", "Pair more", engine=db_engine)
    assert (item.author_id, item.week, item.category, item.text, item.anonymous) == (ann.id, WEEK, "start", "Pair more", False)
    (row,) = rows(db_engine)
    assert (row.id, row.author_id, row.week, row.category, row.text, row.anonymous) == (item.id, ann.id, WEEK, "start", "Pair more", 0)


def test_default_engine(db_engine, users):
    ann, _, _ = users
    add_item(ann.id, WEEK, "stop", "x")
    assert len(rows(db_engine)) == 1


def test_anonymous_true(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "start", "x", anonymous=True, engine=db_engine)
    assert item.anonymous is True
    assert rows(db_engine)[0].anonymous == 1


@pytest.mark.parametrize("category", CATEGORIES)
def test_valid_categories(db_engine, users, category):
    ann, _, _ = users
    assert add_item(ann.id, WEEK, category, "x", engine=db_engine).category == category


@pytest.mark.parametrize("category", ["Start", "STOP", "other", "", " start", "starts"])
def test_bad_category(db_engine, users, category):
    ann, _, _ = users
    with pytest.raises(ValueError):
        add_item(ann.id, WEEK, category, "x", engine=db_engine)
    assert rows(db_engine) == []


def test_text_is_trimmed(db_engine, users):
    ann, _, _ = users
    assert add_item(ann.id, WEEK, "start", "  \t hello world \t ", engine=db_engine).text == "hello world"
    assert rows(db_engine)[0].text == "hello world"


@pytest.mark.parametrize("bad", ["", "   ", "\t  "])
def test_empty_text(db_engine, users, bad):
    ann, _, _ = users
    with pytest.raises(InvalidTextError):
        add_item(ann.id, WEEK, "start", bad, engine=db_engine)
    assert rows(db_engine) == []


def test_length_limit(db_engine, users):
    ann, _, _ = users
    assert len(add_item(ann.id, WEEK, "start", "a" * 280, engine=db_engine).text) == 280
    with pytest.raises(InvalidTextError):
        add_item(ann.id, WEEK, "start", "a" * 281, engine=db_engine)
    assert len(rows(db_engine)) == 1


def test_length_counted_after_trimming(db_engine, users):
    ann, _, _ = users
    add_item(ann.id, WEEK, "start", "  " + "a" * 280 + "  ", engine=db_engine)
    with pytest.raises(InvalidTextError):
        add_item(ann.id, WEEK, "start", " " + "a" * 281 + " ", engine=db_engine)


def test_length_counts_characters_not_bytes(db_engine, users):
    ann, _, _ = users
    add_item(ann.id, WEEK, "start", "é" * 280, engine=db_engine)


@pytest.mark.parametrize("sep", ["\n", "\r", "\r\n", " ", " ", "\v", "\f", "\x85"])
def test_line_breaks_rejected(db_engine, users, sep):
    ann, _, _ = users
    with pytest.raises(InvalidTextError):
        add_item(ann.id, WEEK, "start", f"one{sep}two", engine=db_engine)
    with pytest.raises(InvalidTextError):
        add_item(ann.id, WEEK, "start", f"one two{sep}", engine=db_engine)
    assert rows(db_engine) == []


def test_text_stored_exactly(db_engine, users):
    ann, _, _ = users
    for raw in ["<b>hi</b> & <script>x</script>", "Café ☕ 日本語 😀", "a  b   c"]:
        item = add_item(ann.id, WEEK, "start", raw, engine=db_engine)
        assert item.text == raw
    assert [r.text for r in rows(db_engine)] == ["<b>hi</b> & <script>x</script>", "Café ☕ 日本語 😀", "a  b   c"]


def test_many_items_and_duplicates(db_engine, users):
    ann, _, _ = users
    ids = {add_item(ann.id, WEEK, "start", "same", engine=db_engine).id for _ in range(5)}
    assert len(ids) == 5
    assert len(rows(db_engine)) == 5


def test_unknown_user_stores_nothing(db_engine, users):
    with pytest.raises(UnknownUserError):
        add_item(9999, WEEK, "start", "x", engine=db_engine)
    assert rows(db_engine) == []
    # engine still usable afterwards
    ann, _, _ = users
    add_item(ann.id, WEEK, "start", "x", engine=db_engine)
    assert len(rows(db_engine)) == 1


def test_edit_changes_text_and_updated_at_only(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "stop", "old", engine=db_engine)
    before = rows(db_engine)[0]
    edited = edit_item(item.id, ann.id, "  new <i>text</i> é  ", engine=db_engine)
    after = rows(db_engine)[0]
    assert edited.text == after.text == "new <i>text</i> é"
    assert after.updated_at != before.updated_at
    assert after.updated_at > before.updated_at
    assert after.created_at == before.created_at
    assert (after.category, after.week, after.author_id, after.anonymous) == ("stop", WEEK, ann.id, 0)


def test_edit_twice_updates_each_time(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "stop", "a", engine=db_engine)
    edit_item(item.id, ann.id, "b", engine=db_engine)
    first = rows(db_engine)[0].updated_at
    edit_item(item.id, ann.id, "c", engine=db_engine)
    assert rows(db_engine)[0].updated_at > first


@pytest.mark.parametrize("bad", ["", "  ", "a" * 281, "x\ny", "x y"])
def test_edit_invalid_text_changes_nothing(db_engine, users, bad):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "start", "keep", engine=db_engine)
    before = rows(db_engine)
    with pytest.raises(InvalidTextError):
        edit_item(item.id, ann.id, bad, engine=db_engine)
    assert rows(db_engine) == before


def test_edit_accepts_exactly_280(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "start", "x", engine=db_engine)
    assert len(edit_item(item.id, ann.id, "b" * 280, engine=db_engine).text) == 280


def test_delete_removes_only_that_item(db_engine, users):
    ann, _, _ = users
    a = add_item(ann.id, WEEK, "start", "a", engine=db_engine)
    b = add_item(ann.id, WEEK, "start", "b", engine=db_engine)
    delete_item(a.id, ann.id, engine=db_engine)
    assert [r.id for r in rows(db_engine)] == [b.id]


def test_set_anonymous_on_off_and_idempotent(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "start", "a", engine=db_engine)
    assert set_anonymous(item.id, ann.id, True, engine=db_engine).anonymous is True
    assert set_anonymous(item.id, ann.id, True, engine=db_engine).anonymous is True
    assert rows(db_engine)[0].anonymous == 1
    set_anonymous(item.id, ann.id, False, engine=db_engine)
    set_anonymous(item.id, ann.id, False, engine=db_engine)
    after = rows(db_engine)[0]
    assert after.anonymous == 0
    assert after.text == "a"


@pytest.mark.parametrize("who", ["bob", "owner"])
def test_non_author_cannot_change(db_engine, users, who):
    ann, bob, owner = users
    other = {"bob": bob, "owner": owner}[who]
    item = add_item(ann.id, WEEK, "start", "mine", engine=db_engine)
    before = rows(db_engine)
    with pytest.raises(NotAuthorError):
        edit_item(item.id, other.id, "hacked", engine=db_engine)
    with pytest.raises(NotAuthorError):
        set_anonymous(item.id, other.id, True, engine=db_engine)
    with pytest.raises(NotAuthorError):
        delete_item(item.id, other.id, engine=db_engine)
    assert rows(db_engine) == before


def test_unknown_item(db_engine, users):
    ann, _, _ = users
    with pytest.raises(ItemNotFoundError):
        edit_item(999, ann.id, "x", engine=db_engine)
    with pytest.raises(ItemNotFoundError):
        delete_item(999, ann.id, engine=db_engine)
    with pytest.raises(ItemNotFoundError):
        set_anonymous(999, ann.id, True, engine=db_engine)


def test_deleted_item_is_not_found(db_engine, users):
    ann, _, _ = users
    item = add_item(ann.id, WEEK, "start", "a", engine=db_engine)
    delete_item(item.id, ann.id, engine=db_engine)
    with pytest.raises(ItemNotFoundError):
        delete_item(item.id, ann.id, engine=db_engine)


def test_list_groups_by_category_oldest_first(db_engine, users):
    ann, _, _ = users
    s1 = add_item(ann.id, WEEK, "start", "s1", engine=db_engine)
    t1 = add_item(ann.id, WEEK, "stop", "t1", engine=db_engine)
    s2 = add_item(ann.id, WEEK, "start", "s2", engine=db_engine)
    c1 = add_item(ann.id, WEEK, "continue", "c1", engine=db_engine)
    s3 = add_item(ann.id, WEEK, "start", "s3", engine=db_engine)
    edit_item(s1.id, ann.id, "s1 edited", engine=db_engine)  # editing does not reorder
    grouped = list_items(ann.id, WEEK, engine=db_engine)
    assert list(grouped) == ["start", "stop", "continue"]
    assert [i.id for i in grouped["start"]] == [s1.id, s2.id, s3.id]
    assert [i.id for i in grouped["stop"]] == [t1.id]
    assert [i.id for i in grouped["continue"]] == [c1.id]


def test_list_orders_by_created_at_then_id(db_engine, users):
    ann, _, _ = users
    a = add_item(ann.id, WEEK, "start", "a", engine=db_engine)
    b = add_item(ann.id, WEEK, "start", "b", engine=db_engine)
    c = add_item(ann.id, WEEK, "start", "c", engine=db_engine)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE feedback_items SET created_at = '2020-01-01 00:00:00' WHERE id IN (:b, :c)"), {"b": b.id, "c": c.id})
        conn.execute(text("UPDATE feedback_items SET created_at = '2020-01-01 00:00:00.500000' WHERE id = :a"), {"a": a.id})
    assert [i.id for i in list_items(ann.id, WEEK, engine=db_engine)["start"]] == [b.id, c.id, a.id]


def test_list_only_own_items_and_week(db_engine, users):
    ann, bob, owner = users
    mine = add_item(ann.id, WEEK, "start", "mine", engine=db_engine)
    add_item(bob.id, WEEK, "start", "bobs", engine=db_engine)
    add_item(owner.id, WEEK, "stop", "owners", engine=db_engine)
    add_item(ann.id, "2026-01-12", "start", "other week", engine=db_engine)
    grouped = list_items(ann.id, WEEK, engine=db_engine)
    assert [i.id for items in grouped.values() for i in items] == [mine.id]


def test_list_empty(db_engine, users):
    ann, _, _ = users
    assert list_items(ann.id, WEEK, engine=db_engine) == {"start": [], "stop": [], "continue": []}
    assert list_items(9999, WEEK, engine=db_engine) == {"start": [], "stop": [], "continue": []}


def test_list_default_engine(db_engine, users):
    ann, _, _ = users
    add_item(ann.id, WEEK, "start", "x", engine=db_engine)
    assert len(list_items(ann.id, WEEK)["start"]) == 1
