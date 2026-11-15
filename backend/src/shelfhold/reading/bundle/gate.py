"""At most N blocks at a time for the instance, and one for a token (ADR-0053 §8).

A slot is taken before the first byte and given back when the stream ends, fails, is closed or is
dropped. Two slow readers (or a stolen token and a child's) cannot hold the whole instance: the
stream is also cut below a minimal rate (see `stream.py`), and a refusal tells how long to wait.
"""

import threading
from collections.abc import Callable

from shelfhold.reading.bundle.errors import BundleBusyError

RETRY_AFTER_S = 30


class Gate:
    def __init__(self, *, per_token: int, per_instance: int) -> None:
        self._per_token, self._per_instance = per_token, per_instance
        self._lock = threading.Lock()
        self._total = 0
        self._by_token: dict[str, int] = {}

    def acquire(self, token_id: str | None) -> Callable[[], None]:
        with self._lock:
            if self._total >= self._per_instance:
                raise BundleBusyError(RETRY_AFTER_S)
            if token_id is not None and self._by_token.get(token_id, 0) >= self._per_token:
                raise BundleBusyError(RETRY_AFTER_S)
            self._total += 1
            if token_id is not None:
                self._by_token[token_id] = self._by_token.get(token_id, 0) + 1
        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                self._total -= 1
                if token_id is not None:
                    left = self._by_token.get(token_id, 1) - 1
                    if left <= 0:
                        self._by_token.pop(token_id, None)
                    else:
                        self._by_token[token_id] = left

        return release
