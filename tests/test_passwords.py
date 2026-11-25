import pytest

from app import passwords
from app.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    hash_password,
    verify_password,
)


def test_hash_is_not_plaintext():
    h = hash_password("correct horse")
    assert h != "correct horse"
    assert "correct horse" not in h


def test_same_password_hashes_differently():
    assert hash_password("correct horse") != hash_password("correct horse")


def test_verify_matches_and_rejects_others():
    h = hash_password("correct horse")
    assert verify_password("correct horse", h) is True
    assert verify_password("Correct Horse", h) is False
    assert verify_password("correct horse ", h) is False
    assert verify_password("other password", h) is False


@pytest.mark.parametrize("bad", ["", "   ", "not-a-hash", "$argon2id$garbage"])
def test_verify_false_for_empty_or_malformed_hash(bad):
    assert verify_password("correct horse", bad) is False


def test_non_ascii_and_spaces_roundtrip_untrimmed():
    pw = "pässwörd with spaces ✓"
    h = hash_password(pw)
    assert verify_password(pw, h) is True
    assert verify_password(pw.upper(), h) is False
    assert verify_password(" " + pw, h) is False
    padded = "  padded pw  "
    ph = hash_password(padded)
    assert verify_password(padded, ph) is True
    assert verify_password(padded.strip(), ph) is False


def test_constants_are_named():
    assert passwords.MIN_PASSWORD_LENGTH == 8
    assert passwords.MAX_PASSWORD_LENGTH == 128


def test_length_boundaries():
    with pytest.raises(WeakPasswordError):
        hash_password("a" * (MIN_PASSWORD_LENGTH - 1))
    assert hash_password("a" * MIN_PASSWORD_LENGTH)
    assert hash_password("a" * MAX_PASSWORD_LENGTH)
    with pytest.raises(WeakPasswordError):
        hash_password("a" * (MAX_PASSWORD_LENGTH + 1))


@pytest.mark.parametrize("pw", ["", " ", "       "])
def test_empty_and_short_whitespace_rejected(pw):
    with pytest.raises(WeakPasswordError):
        hash_password(pw)


def test_exception_message_has_no_password():
    with pytest.raises(WeakPasswordError) as e:
        hash_password("secret")
    assert "secret" not in str(e.value)
