"""Username validation and salted password hashing for local accounts."""

import hashlib
import hmac
import re
import secrets
import unicodedata

ITERATIONS = 600_000
SCHEME = "pbkdf2_sha256"


def normalize_username(username):
    """Accept 3-32 ASCII letters, digits or underscores; store lowercase."""
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{3,32}", username):
        raise ValueError("Username must be 3-32 ASCII letters, digits or underscores.")
    return username.lower()


def validate_password(password):
    """Accept 12-128 characters without trimming, normalization or truncation."""
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError("Password must be 12-128 characters.")
    if any(unicodedata.category(char).startswith("C") for char in password):
        raise ValueError("Password must not contain control or other non-printable characters.")


def hash_password(password):
    """Encode the scheme, work factor, random salt and derived key."""
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{SCHEME}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, encoded):
    """Compare derived keys in constant time; unknown users still perform a KDF."""
    validate_password(password)
    if encoded is None:
        encoded = f"{SCHEME}${ITERATIONS}${'00' * 16}${'00' * 32}"
    try:
        scheme, iterations, salt, expected = encoded.split("$")
        if scheme != SCHEME or int(iterations) != ITERATIONS:
            return False
        salt, expected = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
