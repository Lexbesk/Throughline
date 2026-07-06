"""Password hashing (v4 M17): argon2id via argon2-cffi — never plaintext.

argon2id is the current OWASP first-choice password hash; the library's
defaults (memory/iterations/salt) are maintained upstream, which is exactly
what we want to inherit.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

_hasher = PasswordHasher()

# Verified against when a username doesn't exist, so "unknown user" and "wrong
# password" burn comparable time (avoids trivial user enumeration by timing).
DUMMY_HASH = PasswordHasher().hash("throughline-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False
