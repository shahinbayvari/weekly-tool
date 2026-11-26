import pytest
from sqlalchemy import text

from app.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, WeakPasswordError, verify_password
from app.users import (
    DuplicateEmailError,
    InvalidDisplayNameError,
    InvalidEmailError,
    create_user,
    default_display_name,
    find_user_by_email,
    validate_display_name,
)

PW = "correct horse"


def rows(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT id, email, password_hash, role FROM users ORDER BY id")).all()


def test_create_user_returns_normalised_user(db_engine):
    user = create_user("  Ann@Example.COM ", PW, "owner", engine=db_engine)
    assert user.email == "ann@example.com"
    assert user.role == "owner"
    assert verify_password(PW, user.password_hash)
    assert [r.email for r in rows(db_engine)] == ["ann@example.com"]


def test_member_role_and_default_engine(db_engine):
    # no engine argument: uses WEEKLY_DB_PATH set by the fixture
    user = create_user("bob@example.com", PW, "member")
    assert user.role == "member"
    assert len(rows(db_engine)) == 1


@pytest.mark.parametrize("email", ["", "   ", "no-at-sign", "  nope  "])
def test_invalid_email(db_engine, email):
    with pytest.raises(InvalidEmailError):
        create_user(email, PW, "member", engine=db_engine)
    assert rows(db_engine) == []


@pytest.mark.parametrize("dup", ["ann@example.com", "ANN@Example.com", "  ann@example.com  "])
def test_duplicate_email_by_case_and_whitespace(db_engine, dup):
    create_user("ann@example.com", PW, "owner", engine=db_engine)
    before = rows(db_engine)
    with pytest.raises(DuplicateEmailError):
        create_user(dup, "another password", "member", engine=db_engine)
    assert rows(db_engine) == before  # no new row, first user unchanged
    assert before[0].role == "owner"
    assert verify_password(PW, before[0].password_hash)


@pytest.mark.parametrize("role", ["admin", "", "Owner"])
def test_invalid_role(db_engine, role):
    with pytest.raises(ValueError):
        create_user("a@b.com", PW, role, engine=db_engine)
    assert rows(db_engine) == []


def test_weak_password_boundaries(db_engine):
    for pw in ["", "       ", "a" * (MIN_PASSWORD_LENGTH - 1), "a" * (MAX_PASSWORD_LENGTH + 1)]:
        with pytest.raises(WeakPasswordError):
            create_user("a@b.com", pw, "member", engine=db_engine)
    assert rows(db_engine) == []
    create_user("min@b.com", "a" * MIN_PASSWORD_LENGTH, "member", engine=db_engine)
    create_user("max@b.com", "a" * MAX_PASSWORD_LENGTH, "member", engine=db_engine)
    assert len(rows(db_engine)) == 2


def test_no_plaintext_anywhere(db_engine):
    secret = "Sup3r-secret-pässwort"
    user = create_user("ann@example.com", secret, "owner", engine=db_engine)
    assert secret not in repr(user)
    assert secret not in str(user)
    for r in rows(db_engine):
        assert all(secret not in str(v) for v in r)
    errors = []
    for args in [("ann@example.com", secret, "owner"), ("bad", secret, "owner"), ("c@d.com", secret, "boss")]:
        with pytest.raises(ValueError) as e:
            create_user(*args, engine=db_engine)
        errors.append(str(e.value))
    with pytest.raises(WeakPasswordError) as e:
        create_user("c@d.com", "x" * 3, "owner", engine=db_engine)
    errors.append(str(e.value))
    with pytest.raises(WeakPasswordError) as e:
        create_user("c@d.com", secret + "x" * MAX_PASSWORD_LENGTH, "owner", engine=db_engine)
    errors.append(str(e.value))
    assert all(secret not in m for m in errors)


def test_find_user_by_email(db_engine):
    created = create_user("ann@example.com", PW, "owner", engine=db_engine)
    found = find_user_by_email("ANN@example.com ", engine=db_engine)
    assert found is not None and found.id == created.id


@pytest.mark.parametrize("email", ["nobody@example.com", "", "   "])
def test_find_unknown_or_empty_returns_none(db_engine, email):
    create_user("ann@example.com", PW, "owner", engine=db_engine)
    assert find_user_by_email(email, engine=db_engine) is None


def test_create_user_makes_a_current_hash(db_engine):
    from app.passwords import needs_rehash
    from app.users import create_user

    user = create_user("zed@example.com", "correct horse", "member", engine=db_engine)
    assert needs_rehash(user.password_hash) is False


# --- display names (#29) ---------------------------------------------------------


def names(engine):
    with engine.connect() as c:
        return [r[0] for r in c.execute(text("SELECT display_name FROM users ORDER BY id"))]


def test_create_user_default_name_is_the_email_local_part(db_engine):
    user = create_user("  Ann.Lee@Example.COM ", PW, "member", engine=db_engine)
    assert user.display_name == "ann.lee"  # emails are stored lower-cased
    assert names(db_engine) == ["ann.lee"]


def test_create_user_default_name_cut_to_50(db_engine):
    create_user("x" * 60 + "@example.com", PW, "member", engine=db_engine)
    assert names(db_engine) == ["x" * 50]


def test_create_user_default_name_falls_back_to_user_id(db_engine):
    create_user("@example.com", PW, "member", engine=db_engine)
    create_user("Anonymous@example.com", PW, "member", engine=db_engine)
    assert names(db_engine) == ["User 1", "User 2"]


def test_create_user_explicit_name_is_validated_and_trimmed(db_engine):
    assert create_user("a@example.com", PW, "member", display_name="  Sam  ", engine=db_engine).display_name == "Sam"
    for i, bad in enumerate(("", "  ", "anonymous", "a\nb", "x" * 51)):
        with pytest.raises(InvalidDisplayNameError):
            create_user(f"bad{i}@example.com", PW, "member", display_name=bad, engine=db_engine)
    assert names(db_engine) == ["Sam"]


def test_duplicate_names_allowed_and_duplicate_email_still_rejected(db_engine):
    create_user("a@example.com", PW, "member", display_name="Sam", engine=db_engine)
    create_user("b@example.com", PW, "member", display_name="Sam", engine=db_engine)
    with pytest.raises(DuplicateEmailError):
        create_user("a@example.com", PW, "member", display_name="Other", engine=db_engine)
    assert names(db_engine) == ["Sam", "Sam"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Sam", "Sam"),
        ("  Sam  ", "Sam"),
        ("Sam  Lee", "Sam  Lee"),
        ("x" * 50, "x" * 50),
        ("\U0001f600" * 50, "\U0001f600" * 50),
        ("Zoë 李雷", "Zoë 李雷"),
        ("Anonymous Al", "Anonymous Al"),
        ("Anonymou", "Anonymou"),
        ("\U0001f468\u200d\U0001f469", "\U0001f468\u200d\U0001f469"),  # a zero-width joiner inside a name is fine
        ("<b>x</b>", "<b>x</b>"),
        ("Sam\n", "Sam"),  # trailing whitespace is trimmed, not rejected
    ],
)
def test_validate_display_name_accepts(raw, expected):
    assert validate_display_name(raw) == expected


@pytest.mark.parametrize(
    "raw,message",
    [
        ("", "Enter a name"),
        (" \t\n ", "Enter a name"),
        ("\u200b", "Enter a name"),
        ("\u200b \ufeff\u2060", "Enter a name"),
        ("\u00a0\u3000", "Enter a name"),
        ("x" * 51, "Name must be at most 50 characters"),
        ("\U0001f600" * 51, "Name must be at most 50 characters"),
        ("a\nb", "Name must be a single line"),
        ("a\rb", "Name must be a single line"),
        ("a\tb", "Name must be a single line"),
        ("a\x00b", "Name must be a single line"),
        ("a\x1fb", "Name must be a single line"),
        ("a\x7fb", "Name must be a single line"),
        ("a\x85b", "Name must be a single line"),
        ("a\x9fb", "Name must be a single line"),
        ("a\u2028b", "Name must be a single line"),
        ("a\u2029b", "Name must be a single line"),
        ("anonymous", "Choose a different name"),
        ("ANONYMOUS", "Choose a different name"),
        (" Anonymous ", "Choose a different name"),
        ("Anon\u200bymous", "Choose a different name"),
    ],
)
def test_validate_display_name_rejects(raw, message):
    with pytest.raises(InvalidDisplayNameError) as info:
        validate_display_name(raw)
    assert str(info.value) == message


@pytest.mark.parametrize(
    "email,uid,expected",
    [
        ("ann@example.com", 3, "ann"),
        ("ann.lee+tag@example.com", 3, "ann.lee+tag"),
        ("@example.com", 7, "User 7"),
        ("anonymous@example.com", 8, "User 8"),
        ("ANONYMOUS@example.com", 8, "User 8"),
        ("x" * 80 + "@e.com", 1, "x" * 50),
        ("no-at-sign", 2, "no-at-sign"),
    ],
)
def test_default_display_name(email, uid, expected):
    assert default_display_name(email, uid) == expected
