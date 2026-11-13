"""`tombstone lift` (06 Q54, EF-BIB-054 (7)): the way out of a block that the admin decides.

The form (fixed here, with the action of the UI): one tombstone, named by its sequence number
(`seq`, a number that says nothing of what was purged), lifted by an admin who types the phrase
`lift <seq>`. It is a reserved action (a fresh session or the host CLI, never a key or a token) and
it is audited with its actor and the sequence, never a value or a title.

A lift is **a line of its own**, appended to the table and to every journal like a tombstone (the
same values and the same cited versions, marked `lift`): the journals are what survives a restore of
`/data`, and a lift that lived in the table only would be undone by the next replay. The tombstone
it lifts stays where it is (the log is append only); from the lift on, the index does not hold it,
so it blocks nothing. It restores nothing: no Unit, no file, no request comes back by itself. A
purge of the same content afterwards lays a new tombstone, which blocks again.

The lift moves the `?since=` floor like the purges do (`invalidate_tokens`): a client that holds a
token from before resynchronises.
"""

from typing import Final

from shelfhold.library.tombstones.index import LIFT_MARK

__all__ = [
    "LIFT_MARK",
    "LIFT_REASON",
    "LiftConfirmationError",
    "LiftRefusedError",
    "UnknownTombstoneError",
    "lift_phrase",
]

LIFT_REASON: Final = "lifted"


def lift_phrase(seq: int) -> str:
    """The word to type: it names the sequence, so that a phrase typed for another one, or a
    habit, lifts nothing."""
    return f"lift {seq}"


class LiftRefusedError(PermissionError):
    """The reserved-action rule refused the caller (a key, a token, a member, a stale session)."""

    def __init__(self, verdict: object) -> None:
        super().__init__(f"tombstone.lift refused: {type(verdict).__name__}")
        self.verdict = verdict


class LiftConfirmationError(ValueError):
    """The typed word is not the phrase of this sequence: nothing was written."""

    code = "tombstone.confirmation_required"


class UnknownTombstoneError(LookupError):
    """No tombstone holds that sequence, or it is a lift, or it is lifted already."""

    code = "tombstone.unknown"
