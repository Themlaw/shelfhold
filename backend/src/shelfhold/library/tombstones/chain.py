"""The chained label of a root's journal, as pure rules (EF-BIB-054 (6), M1b L26, D12).

Each root keeps, in the database and in its authenticated sentinel, `(seq, count, label)`: the
last `seq` of its journal, how many distinct `seq` it holds and the label chained over them
(`chain_step`, in `seq` order). A journal that no longer reaches that state was truncated.

The file order is not part of it: reordering or repeating lines is harmless. Only lines that open
under their root count (a forged line is no evidence either way). The database is the authority;
the sentinel is the copy a restored database falls back on, and it is believed only when its MAC
verifies under a key we hold that is not compromised.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from shelfhold.infrastructure.secrets.tombstone_aead import TAG_BYTES, chain_step

__all__ = [
    "EMPTY",
    "ZERO_LABEL",
    "ChainState",
    "judge",
    "points_of",
    "tag_of",
    "tip_of",
]

ZERO_LABEL: Final = "A" * 43  # the label of no line at all (43 characters of base64url)


@dataclass(frozen=True)
class ChainState:
    seq: int  # the last seq of the journal
    count: int  # how many distinct seq it holds
    label: str
    # Set while a `rekey` is rewriting the file: the label the new file will have. A kill between
    # the `rename` and the end of the rewrite leaves the file under the new label, which is then
    # as good as the old one.
    pending: str | None = None

    def to_json(self) -> dict[str, object]:
        body: dict[str, object] = {"seq": self.seq, "count": self.count, "label": self.label}
        if self.pending is not None:
            body["pending"] = self.pending
        return body

    @classmethod
    def from_json(cls, raw: object) -> "ChainState | None":
        """None when the value is not a state: an absent or damaged record is no record."""
        if not isinstance(raw, Mapping):
            return None
        seq, count, label, pending = (raw.get(k) for k in ("seq", "count", "label", "pending"))
        for number in (seq, count):
            if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number < 2**63:
                return None
        if not isinstance(label, str) or not 0 < len(label) <= 64:
            return None
        if pending is not None and (not isinstance(pending, str) or not 0 < len(pending) <= 64):
            return None
        return cls(int(seq), int(count), label, pending)  # type: ignore[arg-type]

    def advanced(self, seq: int, tag: bytes) -> "ChainState":
        """One more line, with a `seq` above every one so far."""
        return ChainState(seq, self.count + 1, chain_step(self.label, seq, tag))


EMPTY: Final = ChainState(0, 0, ZERO_LABEL)


def tag_of(ct: bytes) -> bytes:
    """The AEAD tag of a line: the last 16 bytes of its ciphertext."""
    return ct[-TAG_BYTES:]


@dataclass
class Points:
    """The chain of a set of lines at each `seq` they hold (ascending)."""

    by_seq: dict[int, ChainState] = field(default_factory=dict)

    def tip(self) -> ChainState:
        return self.by_seq[max(self.by_seq)] if self.by_seq else EMPTY


def points_of(tags: Mapping[int, bytes]) -> Points:
    """`seq -> state` for lines given as `seq -> tag` (one tag per seq: the caller keeps the
    smallest of several, so that a repeated line is one line)."""
    points = Points()
    state = EMPTY
    for seq in sorted(tags):
        state = state.advanced(seq, tags[seq])
        points.by_seq[seq] = state
    return points


def tip_of(tags: Mapping[int, bytes]) -> ChainState:
    return points_of(tags).tip()


def reached(expected: ChainState, points: Points) -> bool:
    """The journal holds the line `expected.seq` with the label the state recorded, which by
    the chain means every line before it too."""
    if expected.seq == 0:
        return True
    found = points.by_seq.get(expected.seq)
    if found is None:
        return False
    return found.label == expected.label or (
        expected.pending is not None and found.label == expected.pending
    )


def judge(
    tags: Mapping[int, bytes], database: ChainState | None, sentinel: ChainState | None
) -> bool:
    """True when the journal was truncated: it does not reach a state it is held to.

    The database's state binds the journal. The sentinel's binds it only where it is ahead of the
    database (a database restored from before the last purges, or one that never knew the
    journal): where it is level or behind, a mismatch is a copy that lags (a kill between the two
    writes), not evidence."""
    points = points_of(tags)
    if database is not None and not reached(database, points):
        return True
    if sentinel is not None and (database is None or sentinel.seq > database.seq):
        return not reached(sentinel, points)
    return False
