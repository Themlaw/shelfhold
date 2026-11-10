"""Physical roots and the mount sentinel, as pure rules (EF-EXP-029, EF-EXP-059, ENF-OPS-013).

A physical root is a distinct file system under (or beside) /library. Each read-write root has
its own `.shelfhold-mount-id`, `.shelfhold/staging` and `.shelfhold/recycle`. The sentinel says
which instance last wrote the root and how many write sessions it has seen; comparing it to the
database tells an intact root from an emptied mount point or an old copy of the folder.
"""

import json
import posixpath
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from shelfhold.export.domain.path_map import PathEntry, PathMap, clean_absolute, within

__all__ = [
    "LEASE_FILE",
    "MAX_RESTORE_EPOCH",
    "RECEIVED_DIR",
    "RECYCLE_DIR",
    "RESERVED_DIR",
    "SENTINEL_CHAIN_FIELD",
    "SENTINEL_EPOCH_FIELD",
    "SENTINEL_FILE",
    "SENTINEL_KIDS_FIELD",
    "SENTINEL_PREDECESSORS_FIELD",
    "SENTINEL_SCHEMA_VERSION",
    "STAGING_DIR",
    "TOMBSTONE_JOURNAL",
    "ExpectedRoot",
    "RootSpec",
    "RootState",
    "SentinelDocument",
    "SentinelFormatError",
    "SentinelVerdict",
    "discover_physical_roots",
    "is_received_rel",
    "is_reserved",
    "judge_sentinel",
    "takeover_document",
    "waiting_state_of",
]

SENTINEL_FILE = ".shelfhold-mount-id"
RESERVED_DIR = ".shelfhold"
LEASE_FILE = f"{RESERVED_DIR}/lease"  # the per-root write lease (ENF-OPS-015)
STAGING_DIR = f"{RESERVED_DIR}/staging"
RECYCLE_DIR = f"{RESERVED_DIR}/recycle"
RECEIVED_DIR = f"{RESERVED_DIR}/received"  # archives received, before export: never in a library
LEGACY_RECEIVED_DIR = ".received"  # before 0.2.1: it sat in the library, Kavita could scan it
TOMBSTONE_JOURNAL = f"{RESERVED_DIR}/tombstones"  # append-only journal 2.0 (EF-BIB-053)
SENTINEL_KIDS_FIELD = "tombstone_kids"  # registry of issued kids, copied per root (GAP-13-01)
SENTINEL_CHAIN_FIELD = "tombstone_chain"  # last seq, count and chained label of the journal (L26)
SENTINEL_PREDECESSORS_FIELD = "predecessor_instance_ids"  # kept by `root takeover` (EF-EXP-064)
SENTINEL_EPOCH_FIELD = "restore_epoch"  # the restore counter the database had when last stamped
MAX_RESTORE_EPOCH = 2**31  # above this (or unreadable) a sentinel is not trusted: MISMATCH
SENTINEL_SCHEMA_VERSION = "1.0"  # "major.minor", ADR-0039
SUPPORTED_SENTINEL_MAJOR = 1
_VERSION = re.compile(r"^(\d+)\.(\d+)$")
_KNOWN_KEYS = ("schema_version", "root_id", "instance_id", "generation")


class SentinelFormatError(ValueError):
    """The sentinel cannot be read, or is of a newer major format and must not be rewritten."""


class RootState(StrEnum):
    OK = "ok"
    MISSING = "missing"
    MISMATCH = "mismatch"
    STALE = "stale"  # the sentinel is behind the database: an old copy of the folder
    # The database is behind the sentinel (restore, rollback): not a mount_sentinel state, the
    # instance goes to `restored_behind` (EF-INT-026).
    RESTORED_BEHIND = "restored_behind"


@dataclass(frozen=True)
class ExpectedRoot:
    """What the database says about one physical root (`physical_root` and `instance`)."""

    root_id: str
    path: str
    instance_id: str
    generation: int
    # `instance.restore_epoch`: a sentinel stamped under a higher epoch than the database's means
    # the database went back in time, whatever the generation says (EF-INT-026).
    restore_epoch: int = 0


@dataclass(frozen=True)
class SentinelDocument:
    root_id: str
    instance_id: str
    generation: int
    schema_version: str = SENTINEL_SCHEMA_VERSION
    # Fields written by a newer minor version (tombstone counters, kid registry, predecessors,
    # ...) are kept as they are on every rewrite (ENF-OPS-013).
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def major(self) -> int:
        match = _VERSION.match(self.schema_version)
        return int(match.group(1)) if match else 0

    @property
    def read_only(self) -> bool:
        return self.major > SUPPORTED_SENTINEL_MAJOR

    @property
    def restore_epoch(self) -> int:
        """The restore counter stamped with the generation (0 for a sentinel that never had one;
        an unreadable or absurd value is `MAX_RESTORE_EPOCH + 1`: the root is then a MISMATCH)."""
        value = self.extra.get(SENTINEL_EPOCH_FIELD, 0)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_RESTORE_EPOCH
        ):
            return MAX_RESTORE_EPOCH + 1
        return value

    @classmethod
    def parse(cls, raw: bytes) -> Self:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise SentinelFormatError("sentinel is not JSON") from error
        if not isinstance(data, dict):
            raise SentinelFormatError("sentinel is not a JSON object")
        version = data.get("schema_version", SENTINEL_SCHEMA_VERSION)
        root_id, instance_id, generation = (
            data.get("root_id"),
            data.get("instance_id"),
            data.get("generation"),
        )
        if not isinstance(version, str) or not _VERSION.match(version):
            raise SentinelFormatError("bad schema_version")
        if not isinstance(root_id, str) or not isinstance(instance_id, str):
            raise SentinelFormatError("root_id and instance_id must be strings")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise SentinelFormatError("generation must be a non-negative integer")
        extra = {k: v for k, v in data.items() if k not in _KNOWN_KEYS}
        return cls(root_id, instance_id, generation, version, extra)

    def restamped(self, generation: int, *, restore_epoch: int | None = None) -> Self:
        """The same sentinel with a new generation (and, after a restore, a new epoch)."""
        if self.read_only:
            raise SentinelFormatError(
                f"sentinel is format {self.schema_version}, newer than this build: read only"
            )
        extra = dict(self.extra)
        if restore_epoch is not None:
            extra[SENTINEL_EPOCH_FIELD] = restore_epoch
        return type(self)(self.root_id, self.instance_id, generation, self.schema_version, extra)

    def dumps(self) -> bytes:
        body: dict[str, Any] = {
            "schema_version": self.schema_version,
            "root_id": self.root_id,
            "instance_id": self.instance_id,
            "generation": self.generation,
            **self.extra,
        }
        return (json.dumps(body, sort_keys=False, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SentinelVerdict:
    state: RootState
    restamp: bool = False  # the database is one step ahead: rewrite the sentinel, nothing else

    @property
    def blocks_writes(self) -> bool:
        return self.state is not RootState.OK

    @property
    def blocks_scans_and_deletions(self) -> bool:
        return self.state is not RootState.OK


def judge_sentinel(doc: SentinelDocument | None, expected: ExpectedRoot) -> SentinelVerdict:
    """Missing, different, behind or ahead (EF-EXP-029, I39)."""
    if doc is None:
        return SentinelVerdict(RootState.MISSING)
    if doc.read_only:
        return SentinelVerdict(RootState.MISMATCH)  # never touched by a build that cannot read it
    if doc.root_id != expected.root_id or doc.instance_id != expected.instance_id:
        return SentinelVerdict(RootState.MISMATCH)
    if doc.restore_epoch > MAX_RESTORE_EPOCH or not 0 <= doc.generation <= MAX_RESTORE_EPOCH:
        return SentinelVerdict(RootState.MISMATCH)  # forged or damaged: never a reason to freeze
    if doc.restore_epoch > expected.restore_epoch:
        return SentinelVerdict(RootState.RESTORED_BEHIND)  # a restore the database never heard of
    if doc.generation == expected.generation:
        return SentinelVerdict(RootState.OK)
    if doc.generation + 1 == expected.generation:
        # Stopped between the database commit and the sentinel rewrite: consistent.
        return SentinelVerdict(RootState.OK, restamp=True)
    if doc.generation < expected.generation:
        return SentinelVerdict(RootState.STALE)
    return SentinelVerdict(RootState.RESTORED_BEHIND)


def is_received_rel(rel: str) -> bool:
    """`rel` is something under `.shelfhold/received/`: the one reserved folder the follow cycle
    writes to with the ordinary operations (the archives it receives); the rest stays host-only.
    The folder itself is not (three components at least): it cannot be moved or purged."""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    return ".." not in parts and parts[:2] == RECEIVED_DIR.split("/") and len(parts) > 2


def is_reserved(rel: str) -> bool:
    """Host-only names at the top of a root: the sentinel and everything under `.shelfhold/`."""
    parts: list[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return bool(parts) and parts[0] in (SENTINEL_FILE, RESERVED_DIR)


@dataclass(frozen=True)
class RootSpec:
    path: str
    st_dev: int | None  # None: the entry could not be probed (absent)
    entries: tuple[PathEntry, ...]


def discover_physical_roots(
    path_map: PathMap, library_root: str, dev_of: Callable[[str], int]
) -> list[RootSpec]:
    """Group the entries by file system: one root per distinct device (EF-EXP-059).

    Entries on the device of `library_root` share it. Any other device is a root of its own, at
    the common parent of its entries; an entry that cannot be probed is an absent root, never
    folded into the root of its parent folder (an unmounted disk leaves an empty directory).
    """
    library = clean_absolute(library_root)
    try:
        library_dev: int | None = dev_of(library)
    except OSError:
        library_dev = None
    inside: list[PathEntry] = []
    groups: dict[int, list[PathEntry]] = {}
    absent: list[PathEntry] = []
    for entry in path_map.entries:
        try:
            dev = dev_of(entry.local)
        except OSError:
            absent.append(entry)
            continue
        under = within(entry.local, library) is not None
        if under and library_dev is not None and dev == library_dev:
            inside.append(entry)
        else:
            groups.setdefault(dev, []).append(entry)
    roots: list[RootSpec] = []
    if inside and library_dev is not None:
        roots.append(RootSpec(library, library_dev, tuple(inside)))
    for dev, entries in groups.items():
        roots.append(RootSpec(_common_parent(entries), dev, tuple(entries)))
    roots.extend(RootSpec(entry.local, None, (entry,)) for entry in absent)
    return sorted(roots, key=lambda root: root.path)


def _common_parent(entries: Iterable[PathEntry]) -> str:
    return posixpath.commonpath([entry.local for entry in entries])


def waiting_state_of(state: RootState, *, store: bool = False) -> str | None:
    """The state a root that is not right waits in (EF-EXP-064): a sentinel of another instance
    (or an unreadable, forged one) is `foreign_root`, no sentinel on an empty or replaced disk is
    `root_lost`, a root older than the database is `root_behind`; the same three on `/store` are
    `store_lost`. `RESTORED_BEHIND` (the database is older than the disk) is the instance's own
    state (EF-INT-026) and `OK` waits for nothing."""
    if state in (RootState.OK, RootState.RESTORED_BEHIND):
        return None
    if store:
        return "store_lost"
    return {
        RootState.MISMATCH: "foreign_root",
        RootState.MISSING: "root_lost",
        RootState.STALE: "root_behind",
    }[state]


def takeover_document(
    found: SentinelDocument | None,
    *,
    root_id: str,
    instance_id: str,
    generation: int,
    restore_epoch: int,
    predecessors: Iterable[str],
    kids: Sequence[Mapping[str, Any]] = (),
) -> SentinelDocument:
    """The sentinel `root takeover|reinit` stamps (EF-EXP-064, EF-BIB-054 (8)).

    A found sentinel is a claim, never an input: `root_id`, `instance_id`, `generation` and the
    list of predecessors come from the caller (the database, and the evidence it gathered), and
    the only thing taken from the file's extra fields is `kids` (entries `{kid, tags}`), which the
    caller has already authenticated under `root_id`. Every other unknown field of a foreign file
    is dropped, so a forged sentinel
    cannot carry a field into ours. The previous `instance_id` joins the predecessors when it
    differs; the list has no duplicate and keeps its order."""
    chain: list[str] = []
    for value in (*predecessors, found.instance_id if found else None):
        if isinstance(value, str) and value and value != instance_id and value not in chain:
            chain.append(value)
    extra: dict[str, Any] = {
        SENTINEL_EPOCH_FIELD: restore_epoch,
        SENTINEL_PREDECESSORS_FIELD: chain,
    }
    version = SENTINEL_SCHEMA_VERSION
    if kids:
        extra[SENTINEL_KIDS_FIELD] = [dict(item) for item in kids]
        version = "1.1"  # the minor that introduced `tombstone_kids`
    return SentinelDocument(root_id, instance_id, generation, version, extra)
