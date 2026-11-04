"""Opaque identifiers: ULIDs (ENF-OPS-020).

Twenty-six Crockford base32 characters: 48 bits of millisecond time, then 80 bits of randomness.
Within one millisecond the random part is incremented, so identifiers sort by creation order on
a given process. No dependency: the format is fixed and tiny.
"""

import os
import re
import secrets
import threading
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26
_RANDOM_BITS = 80
_ULID_PATTERN = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")

_lock = threading.Lock()
_last_ms = -1
_last_random = 0


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    global _last_ms, _last_random
    with _lock:
        now = time.time_ns() // 1_000_000
        if now > _last_ms:
            _last_ms, _last_random = now, secrets.randbits(_RANDOM_BITS - 1)  # headroom to count up
        else:
            _last_random += 1  # same millisecond (or clock stepped back): keep the order
        return _encode(_last_ms, 10) + _encode(_last_random, 16)


def _reset_after_fork() -> None:
    """A forked worker must not continue the parent's counter (identical ids) nor its lock."""
    global _lock, _last_ms, _last_random
    _lock = threading.Lock()
    _last_ms, _last_random = -1, 0


os.register_at_fork(after_in_child=_reset_after_fork)


def is_ulid(value: str) -> bool:
    return _ULID_PATTERN.fullmatch(value) is not None
