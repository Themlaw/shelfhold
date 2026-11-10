"""Is a sentinel the one of a root whose tombstones we hold? (EF-EXP-064, EF-BIB-054 (8))

`root takeover` stamps a root whose sentinel says another instance, or says nothing we can trust.
The sentinel is a claim: anybody who can write in the root can write any `root_id` into it, and a
`root_id` that is not the one the journal lines were sealed under makes every line fail to open,
which would let purged content come back (`tombstone.unverified` lines are inert). So the
`root_id` a takeover keeps is never read from the file alone. It is *authenticated* when, under
that `root_id`, at least one of these holds with the keyring we have:

- an entry of the sentinel's registry of kids carries a tag that verifies under a key of the
  keyring (the tag is bound to `root_id` and `kid`, EF-BIB-053, R58);
- a line of the journal opens (AEAD, the `root_id` is in the associated data).

Nothing here writes. A root with a journal whose lines cannot be told apart (no key for them)
has `lines > 0` and `opened == 0`: it authenticates nothing, and the caller refuses to guess.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from shelfhold.infrastructure.secrets.errors import EnvelopeError
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import open_values, verify_kid_tag
from shelfhold.library.tombstones.journal import (
    MAX_JOURNAL_BYTES,
    JournalFormatError,
    parse_journal,
)
from shelfhold.library.tombstones.service import JournalDisk

__all__ = ["RootEvidence", "SentinelEvidence"]


@dataclass(frozen=True)
class RootEvidence:
    """What the keyring says about one candidate `root_id` of a root."""

    root_id: str
    kids: frozenset[str]  # kids of the sentinel's registry whose tag verifies
    lines: int  # lines of the journal that parse
    opened: int  # lines that open under this `root_id`
    unreadable: bool = False  # the journal is there and cannot be read

    @property
    def authentic(self) -> bool:
        return bool(self.kids) or self.opened > 0

    @property
    def contradicted(self) -> bool:
        """The journal holds lines, none of which opens under this `root_id`: the id is not the
        one they were sealed for (or the keyring lacks their keys)."""
        return self.unreadable or (self.lines > 0 and self.opened == 0 and not self.kids)


class SentinelEvidence:
    def __init__(self, keyring: Keyring, disk: JournalDisk) -> None:
        self._keyring = keyring
        self._disk = disk

    def examine(self, root_path: str, candidates: Sequence[str]) -> dict[str, RootEvidence]:
        """One `RootEvidence` per candidate `root_id`, from the sentinel registry and the journal
        of the root at `root_path`. Read only."""
        entries = self._disk.tombstone_kids(root_path)
        try:
            raw = self._disk.read_tombstone_journal(root_path, max_bytes=MAX_JOURNAL_BYTES)
            journal = None if not raw else parse_journal(raw)
            unreadable = False
        except (OSError, JournalFormatError):
            journal, unreadable = None, True
        found: dict[str, RootEvidence] = {}
        for candidate in dict.fromkeys(candidates):
            kids = {
                kid
                for kid, tags in entries.items()
                if any(
                    (key := self._keyring.find(signer)) is not None
                    and verify_kid_tag(key.material, candidate, kid, tag)
                    for signer, tag in tags.items()
                )
            }
            opened = 0
            lines = journal.lines if journal else []
            for line in lines:
                if self._keyring.find(line.kid) is None:
                    continue
                try:
                    open_values(
                        self._keyring.get(line.kid).material, line.nonce, line.ct,
                        scope=line.scope, root=candidate, seq=line.seq,
                    )  # fmt: skip
                except (EnvelopeError, ValueError):
                    continue
                opened += 1
            found[candidate] = RootEvidence(
                candidate, frozenset(kids), len(lines), opened, unreadable
            )
        return found
