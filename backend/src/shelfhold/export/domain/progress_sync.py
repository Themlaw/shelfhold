"""Which destinations can synchronise the reading progress of an account (M4 L46).

The sync reads and writes a reading position kept per account on the server: Kavita does
(`progress="api"`). Komga, a drop box and a native folder keep none (`progress="none"`): turning
the switch on for one of them would never do anything, so it is refused, with a code.
"""

from typing import Final

__all__ = ["SYNC_KINDS", "can_sync_progress"]

SYNC_KINDS: Final = frozenset({"kavita"})


def can_sync_progress(kind: str) -> bool:
    return kind in SYNC_KINDS
