"""argon2id through argon2-cffi (P8), with the library's RFC 9106 defaults (t=3, m=64 MiB, p=4).

Not configurable (P1, invisible protection). At most two hashes run at a time: each takes 64 MiB,
and a burst of logins must not exhaust the memory of a small host.
"""

import secrets
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MAX_CONCURRENT_HASHES = 2


class Argon2Hasher:
    def __init__(self) -> None:
        self._hasher = PasswordHasher()  # argon2id
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_HASHES)
        self._dummy: str | None = None
        self._dummy_lock = threading.Lock()

    def hash(self, password: str) -> str:
        with self._slots:
            return self._hasher.hash(password)

    def _dummy_digest(self) -> str:
        with self._dummy_lock:
            if self._dummy is None:
                self._dummy = self.hash(secrets.token_urlsafe(24))
            return self._dummy

    def verify(self, digest: str | None, password: str) -> bool:
        target = digest if digest is not None else self._dummy_digest()
        with self._slots:
            try:
                matched = self._hasher.verify(target, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False
        return bool(matched) and digest is not None

    def needs_rehash(self, digest: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(digest)
        except InvalidHashError:
            return False
