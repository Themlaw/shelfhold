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

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.library.tombstones.index import LIFT_MARK, flatten
from shelfhold.library.tombstones.scopes import folded_fields
from shelfhold.library.tombstones.values import TombstoneValues

__all__ = [
    "EVENT_KEY",
    "LEGACY_TARGET",
    "LIFT_MARK",
    "LIFT_REASON",
    "ORIGIN_KEY",
    "LiftConfirmationError",
    "LiftRefusedError",
    "TombstoneRow",
    "UnknownTombstoneError",
    "lift_cited",
    "lift_phrase",
    "new_event_id",
    "origin_token",
    "origins_of",
    "still_blocking",
    "versions_of",
]

LIFT_REASON: Final = "lifted"

# Every event (a purge, a lift) carries its own identifier in the sealed `cited` of its line: unique
# and ordered (a ULID), written in the table and in every journal. A replay merges by it, never by
# the values: two purges of the same content are two events, and a lift names the one it lifts.
EVENT_KEY: Final = "ev"
ORIGIN_KEY: Final = "from"  # a new form: the tokens of the tombstones it was derived from
LEGACY_TARGET: Final = "1"  # a lift of a tombstone laid before events had an identifier


def new_event_id() -> str:
    return new_ulid()


def event_of(cited: Mapping[str, str]) -> str | None:
    return cited.get(EVENT_KEY)


def versions_of(cited: Mapping[str, str]) -> dict[str, str]:
    """The cited normalizer versions alone (what the index keys on), without the event marks."""
    return {k: v for k, v in cited.items() if k not in (EVENT_KEY, LIFT_MARK, ORIGIN_KEY)}


def origin_token(values: TombstoneValues, cited: Mapping[str, str]) -> str:
    """What a new form says to name the tombstone it comes from: the identifier of its event, or,
    for a tombstone from before the identifiers, a digest of its folded reference. Both live in
    the sealed line: nothing readable is written (EF-BIB-054 (4))."""
    event = event_of(cited)
    if event is not None:
        return event
    folded = folded_fields(values.scope, flatten(values)) or ()
    # the scope is part of the token: a variant and a series binding that fold alike differ
    # (the token is computed on read, so the tombstones that exist are given it at the next start)
    digest = hashlib.sha256("\x1f".join((values.scope, *folded)).encode()).hexdigest()[:16]
    return "~" + digest


def origins_of(cited: Mapping[str, str]) -> frozenset[str]:
    return frozenset(t for t in cited.get(ORIGIN_KEY, "").split(",") if t)


def lift_cited(target: Mapping[str, str]) -> dict[str, str]:
    """The `cited` of the line that lifts `target`: its versions, a new event, and its target."""
    return {
        **versions_of(target), EVENT_KEY: new_event_id(),
        LIFT_MARK: event_of(target) or LEGACY_TARGET,
    }  # fmt: skip


def still_blocking[V](
    entries: Sequence[tuple[V, dict[str, str]]],
) -> list[tuple[V, dict[str, str]]]:
    """What still blocks. Events with an identifier are read in the order of their identifiers and
    a lift takes out exactly the purge it names, whatever the order the lines were met in; a purge
    laid after the lift stays. Lines from before the identifiers keep their old rule: a lift takes
    out the earlier ones of the same values and versions, and so does a lift that has its own
    identifier but names no purge (`LEGACY_TARGET`): the lift of such a line."""
    legacy = [e for e in entries if event_of(e[1]) is None]
    events = sorted(
        (e for e in entries if event_of(e[1]) is not None), key=lambda e: e[1][EVENT_KEY]
    )
    lifted = {e[1][LIFT_MARK] for e in events if LIFT_MARK in e[1]}
    active: list[tuple[V, dict[str, str]]] = []
    for values, cited in legacy:
        if LIFT_MARK in cited:
            base = versions_of(cited)
            active = [a for a in active if (a[0], versions_of(a[1])) != (values, base)]
        else:
            active.append((values, cited))
    for values, cited in events:  # the lift of a pose from before the identifiers
        if cited.get(LIFT_MARK) == LEGACY_TARGET:
            base = versions_of(cited)
            active = [a for a in active if (a[0], versions_of(a[1])) != (values, base)]
    active += [e for e in events if LIFT_MARK not in e[1] and e[1][EVENT_KEY] not in lifted]
    return active


def lift_phrase(seq: int) -> str:
    """The word to type: it names the sequence, so that a phrase typed for another one, or a
    habit, lifts nothing."""
    return f"lift {seq}"


@dataclass(frozen=True)
class TombstoneRow:
    """One line of `tombstone list`: a number to lift by, never what the tombstone stands for."""

    seq: int
    created_at: datetime
    scope: str
    reason: str | None
    linked_to: tuple[int, ...] = ()  # a new form: the sequences of the tombstones it comes from


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
