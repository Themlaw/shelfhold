"""The signing key of the `?since=` tokens of an instance (M3 L11, EF-INT-001).

32 random bytes kept in the data directory (`0600`), created on first use and never logged. A
token is signed under it (`interfaces.http_api.lists`), so a client can neither make one nor edit
one; a restored backup brings its own key with its own sequence.
"""

import contextlib
import secrets
import threading
from pathlib import Path

from shelfhold.infrastructure.secrets.atomic import write_private

__all__ = ["KEY_FILE", "SinceKey"]

KEY_FILE = "since-token.key"


class SinceKey:
    """The signing key of the tokens of an instance: 32 random bytes, kept in the data directory
    (`0600`, created on first use, never logged)."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / KEY_FILE
        self._key: bytes | None = None
        self._lock = threading.Lock()

    def value(self) -> bytes:
        with self._lock:
            if self._key is None:
                with contextlib.suppress(FileExistsError):  # another process made it first
                    write_private(self._path, secrets.token_bytes(32), overwrite=False)
                self._key = self._path.read_bytes()
            return self._key
