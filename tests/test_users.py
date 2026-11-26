import pytest
from sqlalchemy import text

from app.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, WeakPasswordError, verify_password
from app.users import DuplicateEmailError, InvalidEmailError, create_user, find_user_by_email

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
