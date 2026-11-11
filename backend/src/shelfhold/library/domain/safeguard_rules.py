"""When the upstream copy of a Unit may be erased (EF-BIB-038, EF-SRC-009, EF-BIB-042 (4)).

The upstream download is the one thing "Mettre à l'abri" erases that Shelfhold cannot bring back
by itself, so the rule is a closed list of reasons to refuse and a single way to be allowed:

- the shelf asked for it (`purge_upstream_after_export`, off by default);
- nothing is under a tombstone (a buried content is never kept, never erased "to be safe");
- the service is not paused for anything (`source_instance_changed`, an expired secret, a version
  it cannot drive), its roles are known (`roles_unverified` is a service account that might be
  too powerful to trust a delete to) and the upstream source is loaded (`source.not_loaded`);
- the copy in Kavita exists, verified;
- and either a verified copy sits in /store, or the source is alive and nothing owes the Unit a
  copy. A Unit that is fragile, gone or a candidate (EF-BIB-031) and whose copy has not been made
  yet (behind the per-cycle cap, the room of /store, `unit.safeguard_backlog`) is never erased.

Plain data in, a verdict out: the checks themselves are made against the disks and the database
by the caller, again at the moment of the deed (EF-EXP-064).
"""

from dataclasses import dataclass
from typing import Final

OPTION_OFF: Final = "option_off"
TOMBSTONED: Final = "tombstoned"
SERVICE_PAUSED: Final = "service_paused"
ROLES_UNVERIFIED: Final = "roles_unverified"
SOURCE_NOT_LOADED: Final = "source_not_loaded"
NO_KAVITA_COPY: Final = "no_kavita_copy"
NOT_DOWNLOADED: Final = "not_downloaded"
SAFEGUARD_PENDING: Final = "safeguard_pending"
STORE_COPY_MISSING: Final = "store_copy_missing"
STORE_COPY_CHANGED: Final = "store_copy_changed"
DEED_OPEN: Final = "deed_open"  # another erasure of this very download is begun (L17)

REFUSALS: Final = frozenset(
    {
        OPTION_OFF, TOMBSTONED, SERVICE_PAUSED, ROLES_UNVERIFIED, SOURCE_NOT_LOADED,
        NO_KAVITA_COPY, NOT_DOWNLOADED, SAFEGUARD_PENDING, STORE_COPY_MISSING, STORE_COPY_CHANGED,
        DEED_OPEN,
    }
)  # fmt: skip


@dataclass(frozen=True)
class PurgeFacts:
    option_on: bool
    tombstoned: bool
    service_paused: bool
    roles_unverified: bool
    source_loaded: bool
    kavita_copy: bool  # the file Kavita reads exists and is the one that was exported
    store_copy: bool  # a verified independent copy in /store
    source_alive: bool  # an active binding still lists the Unit
    owed: bool  # fragile, gone or candidate: the Unit must be in /store before anything is erased


@dataclass(frozen=True)
class PurgeVerdict:
    allowed: bool
    reason: str | None = None


def upstream_verdict(facts: PurgeFacts) -> PurgeVerdict:
    """The first reason to refuse, in the order that is cheapest to explain; else allowed."""
    if not facts.option_on:
        return PurgeVerdict(False, OPTION_OFF)
    if facts.tombstoned:
        return PurgeVerdict(False, TOMBSTONED)
    if facts.service_paused:
        return PurgeVerdict(False, SERVICE_PAUSED)
    if facts.roles_unverified:
        return PurgeVerdict(False, ROLES_UNVERIFIED)
    if not facts.source_loaded:
        return PurgeVerdict(False, SOURCE_NOT_LOADED)
    if not facts.kavita_copy:
        return PurgeVerdict(False, NO_KAVITA_COPY)
    if facts.store_copy:
        return PurgeVerdict(True)
    if facts.owed:
        return PurgeVerdict(False, SAFEGUARD_PENDING)
    if facts.source_alive:
        return PurgeVerdict(True)
    return PurgeVerdict(
        False, STORE_COPY_MISSING
    )  # no copy of ours and nothing upstream to ask again
