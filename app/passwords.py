"""Password hashing, verification and length rules (argon2-cffi, default parameters)."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

_hasher = PasswordHasher()


class WeakPasswordError(ValueError):
    """The password breaks the length rules. The message never contains the password."""


def validate_password(password: str) -> None:
    """Raise WeakPasswordError if the password is too short or too long. Never trims."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters")


def hash_password(password: str) -> str:
    """Validate the password, then return a salted argon2 hash of it."""
    validate_password(password)
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True only if the password matches. Empty or malformed hashes give False, never an error."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, ValueError, TypeError):
        return False
