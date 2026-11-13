"""The key of the `?since=` tokens of an instance (M3 L11, EF-INT-001).

`K_since = HKDF-SHA256(oldest key of the keyring, info "shelfhold/since/v1")`: same material as the
keyring, another purpose. The keyring is part of every backup, and it keeps its retired keys, so
a restored instance derives the very key that sealed its clients' tokens (a rotation does not
move it: the oldest key stays). A token of a restored instance therefore opens, and its
`restore_epoch` or its history tells the route to answer `resync_required`, not `invalid_cursor`.
The key never leaves this process and is never logged.
"""

import time
from collections.abc import Callable

from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import hkdf_sha256

__all__ = ["KEY_FILE", "SinceKey"]

KEY_FILE = "keyring.age"  # the member of a backup that holds the material (a test pins it)

_INFO = b"shelfhold/since/v1"
_TTL_S = 30.0  # the keyring is a file: derived once in a while, not on every request


class SinceKey:
    SOURCE = KEY_FILE  # where its material lives in a backup

    def __init__(self, keyring: Callable[[], Keyring]) -> None:
        self._keyring = keyring
        self._cached: tuple[float, bytes] | None = None

    def value(self) -> bytes:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > _TTL_S:
            oldest = min(self._keyring().keys, key=lambda k: (k.created_at, k.kid))
            self._cached = (now, hkdf_sha256(oldest.material, info=_INFO))
        return self._cached[1]
