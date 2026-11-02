"""Password hashing (argon2id) and policy."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024

_hasher = PasswordHasher()
# Verified against when the username is unknown, so timing doesn't reveal it.
_DUMMY_HASH = _hasher.hash("mediawrangler-dummy-password")


def password_problem(password: str, confirm: str | None = None) -> str | None:
    """Return a message describing why the password is unacceptable, or None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if password.strip() != password:
        return "Password must not start or end with whitespace."
    if confirm is not None and password != confirm:
        return "Passwords do not match."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
