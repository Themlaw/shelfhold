"""The one entry point for writing under /library (EF-EXP-006, EF-EXP-029, EF-EXP-059, EF-EXP-060).

Callers give absolute local paths (`/library/manga/X/1.cbz`). The path is located lexically in one
physical root, the host-only names are refused, the entry of the path map decides read-only or
not, and the root's sentinel is read again before every write. Only then is the root opened and
the operation done, through descriptors, in the same open root that the sentinel was read from.
Nothing here ever reads a path that was checked by a separate call.
"""

import contextlib
import hashlib
import os
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import BinaryIO, Literal, Self

from shelfhold.export.domain.path_map import (
    PathMap,
    PathMapError,
    PathMode,
    clean_absolute,
    within,
)
from shelfhold.export.domain.physical_roots import (
    LEASE_FILE,
    RECYCLE_DIR,
    STAGING_DIR,
    ExpectedRoot,
    RootState,
    SentinelDocument,
    SentinelVerdict,
    is_received_rel,
    is_reserved,
    judge_sentinel,
)
from shelfhold.export.domain.provenance_mirror import is_mirror_rel
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, Kind, Strategy
from shelfhold.infrastructure.fsops.emitters import (
    TRASH_RETENTION_S,
    Perimeter,
    perimeter_of,
    require_content_inside,
    require_inside,
    require_tree_allowed,
    trash_entry,
)
from shelfhold.infrastructure.fsops.errors import (
    CrossRootError,
    EmitterConditionError,
    EmitterPerimeterError,
    LeaseRaceError,
    NotRegularFileError,
    OutsidePathMapError,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
    SentinelFormatError,
    TombstonePendingError,
)
from shelfhold.infrastructure.fsops.identity import FileIdentity, identity_of
from shelfhold.infrastructure.fsops.mirror import (
    list_mirror_files,
    read_mirror_file,
    write_mirror_file,
)
from shelfhold.infrastructure.fsops.sentinel import read_sentinel, write_sentinel
from shelfhold.infrastructure.fsops.tombstone_journal import (
    add_sentinel_kid,
    append_line,
    read_journal,
    sentinel_kids,
)
from shelfhold.infrastructure.fsops.writer import (
    HashingReader,
    Readable,
    ensure_host_dirs,
    write_atomic,
)

__all__ = ["BootReport", "Fsops", "RootStatus"]


@dataclass(frozen=True)
class RootStatus:
    root_path: str
    state: RootState
    read_only: bool = False


@dataclass
class BootReport:
    ok: list[str] = field(default_factory=list)
    blocked: dict[str, RootState] = field(default_factory=dict)
    restamped: list[str] = field(default_factory=list)
    purged_staging: int = 0
    tombstone_owed: list[str] = field(default_factory=list)  # left alone: lines are due first


def _read_lease(root: ConfinedRoot) -> bytes | None:
    try:
        fd = root.open_file(LEASE_FILE, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        return os.read(fd, 4096)
    finally:
        os.close(fd)


def _claim_lease(root: ConfinedRoot, seen: bytes) -> None:
    """Move the lease aside (atomic: one claimer wins) and check it is the one that was read."""
    aside = f"{LEASE_FILE}.{uuid.uuid4().hex}"
    try:
        root.rename(LEASE_FILE, aside, replace=False, durable=False)
    except FileNotFoundError:
        raise LeaseRaceError("the lease vanished meanwhile") from None
    try:
        fd = root.open_file(aside, os.O_RDONLY)
        try:
            moved = os.read(fd, 4096)
        finally:
            os.close(fd)
        if moved != seen:  # we moved a lease that is not the one we judged: put it back
            with contextlib.suppress(FileExistsError):
                root.link(aside, LEASE_FILE)
            raise LeaseRaceError("the lease changed meanwhile")
    finally:
        with contextlib.suppress(FileNotFoundError):
            root.unlink(aside)


@dataclass(frozen=True)
class _Located:
    root_path: str
    rel: str
    read_only: bool


class Fsops:
    def __init__(
        self,
        path_map: PathMap,
        expected: Sequence[ExpectedRoot],
        *,
        umask: int | None = None,
        strategy: Strategy = Strategy.AUTO,
        write_gate: Callable[[], None] | None = None,
        stop_gate: Callable[[], None] | None = None,
        now: Callable[[], float] = time.time,
        frozen: bool = False,
    ) -> None:
        self._stop_gate = stop_gate  # raises once the shutdown went past its checkpoint delay
        self._write_gate = write_gate  # raises while the disk guard is paused (ENF-OPS-006)
        self._now = now
        self._path_map = path_map
        self._expected = {clean_absolute(root.path): root for root in expected}
        self._read_only = {e.local for e in path_map.entries if e.mode is PathMode.ADOPT_RO}
        self._umask = umask
        self._strategy = strategy
        self._lease_verifier: Callable[[str, bytes | None], None] | None = None
        self._tombstones_owed: Callable[[str], bool] | None = None
        self._frozen = frozen
        self._behind_hook: Callable[[], None] | None = None

    @property
    def frozen(self) -> bool:
        return self._frozen

    def on_restored_behind(self, hook: Callable[[], None] | None) -> None:
        """Called when a write finds a root whose sentinel is ahead of the database (a root that
        came back after the start): the composition root persists the freeze (EF-INT-026)."""
        self._behind_hook = hook

    def freeze(self) -> None:
        """Read-only for the whole instance, every root, until `unfreeze`: the database is older
        than the disk (`restored_behind`, EF-INT-026). Reads, observation and hashing still work;
        no write, deletion, session or scan does."""
        self._frozen = True

    def unfreeze(self) -> None:
        self._frozen = False

    def enforce_lease(self, verify: Callable[[str, bytes | None], None] | None) -> None:
        """From now on every write passes `verify(root_path, raw_lease)`, which raises when the
        root's lease is not this instance's valid one (ENF-OPS-015). Set once at start-up, so no
        write path (journal, replay, sessions) can skip the check."""
        self._lease_verifier = verify

    def enforce_tombstone_flush(self, owed: Callable[[str], bool] | None) -> None:
        """From now on a write to a root for which `owed(root_path)` is true is refused, except
        the tombstone lines themselves: a root that was away during a purge gets its lines
        before anything else (EF-BIB-052). Set once at start-up, like the lease."""
        self._tombstones_owed = owed

    @classmethod
    def open(
        cls,
        path_map: PathMap,
        expected: Sequence[ExpectedRoot],
        *,
        umask: int | None = None,
        strategy: Strategy = Strategy.AUTO,
        write_gate: Callable[[], None] | None = None,
        stop_gate: Callable[[], None] | None = None,
        now: Callable[[], float] = time.time,
        frozen: bool = False,
    ) -> Self:
        """No file system access: roots are opened, and their sentinels read, per operation.
        `frozen`: the database says `restored_behind` (read from `LoadedRoots.restore_pending`)."""
        return cls(
            path_map, expected, umask=umask, strategy=strategy,
            write_gate=write_gate, stop_gate=stop_gate, now=now, frozen=frozen,
        )  # fmt: skip

    # -- locating ---------------------------------------------------------------------------

    def _locate(self, local: str, *, host: bool = False, read_mirror: bool = False) -> _Located:
        try:
            path = clean_absolute(local)
        except PathMapError as error:
            raise PathEscapeError(str(error)) from error
        best: tuple[str, str, bool] | None = None
        for root_path, read_only in [
            *((p, False) for p in self._expected),
            *((p, True) for p in self._read_only),
        ]:
            below = within(path, root_path)
            if below is not None and (best is None or len(root_path) > len(best[0])):
                best = (root_path, below, read_only)
        if best is None:
            raise OutsidePathMapError(f"{local!r} is under no physical root")
        root_path, rel, read_only = best
        received = is_received_rel(rel)  # the archives of the follow cycle, host area, ordinary ops
        if (
            is_reserved(rel)
            and not host
            and not received
            and not (read_mirror and is_mirror_rel(rel))
        ):
            raise ReservedPathError(f"{local!r} is reserved to the host")
        host = host or received or (read_mirror and is_mirror_rel(rel))
        if not read_only and not (host and is_reserved(rel)):  # host folders sit above the map
            try:
                entry = self._path_map.entry_for(path)
            except PathMapError as error:
                raise OutsidePathMapError(f"{local!r} is in no entry of the path map") from error
            if entry.mode is PathMode.ADOPT_RO:  # a read-only entry nested in a writable root
                read_only = True
        return _Located(root_path, rel or ".", read_only)

    @contextmanager
    def _root(self, root_path: str) -> Iterator[ConfinedRoot]:
        try:
            root = ConfinedRoot.open(root_path, strategy=self._strategy, umask=self._umask)
        except OSError as error:
            if root_path in self._read_only:
                raise
            raise RootBlockedError(root_path, RootState.MISSING) from error
        try:
            yield root
        finally:
            root.close()

    def _verdict(self, root: ConfinedRoot) -> SentinelVerdict:
        expected = self._expected[root.path]
        try:
            return judge_sentinel(read_sentinel(root), expected)
        except (SentinelFormatError, OSError):
            # unreadable, a FIFO, a directory...: fail closed, this root only (EF-EXP-059)
            return SentinelVerdict(RootState.MISMATCH)

    @contextmanager
    def _gated(
        self, located: _Located, *, lease: bool = True, tombstone: bool = False
    ) -> Iterator[ConfinedRoot]:
        """The root opened for a write: refused when read-only, when its sentinel says so, when
        the lease is not ours (once `enforce_lease` is on) or when tombstone lines are owed to it
        (`tombstone=True`: the write that pays them). After the shutdown's checkpoint delay every
        write is refused (`stop_gate`)."""
        if self._stop_gate is not None:
            self._stop_gate()
        if located.read_only:
            raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
        if self._frozen:
            raise RootBlockedError(located.root_path, RootState.RESTORED_BEHIND)
        owed = self._tombstones_owed
        if lease and not tombstone and owed is not None and owed(located.root_path):
            raise TombstonePendingError(located.root_path)
        with self._root(located.root_path) as root:
            verdict = self._verdict(root)
            if verdict.blocks_writes:
                if verdict.state is RootState.RESTORED_BEHIND and self._behind_hook is not None:
                    self._behind_hook()
                raise RootBlockedError(located.root_path, verdict.state)
            if lease and self._lease_verifier is not None:
                self._lease_verifier(located.root_path, _read_lease(root))
            yield root

    # -- state ------------------------------------------------------------------------------

    def status(self, local: str) -> RootStatus:
        located = self._locate(local)
        if located.read_only:
            return RootStatus(located.root_path, RootState.OK, read_only=True)
        try:
            with self._root(located.root_path) as root:
                return RootStatus(located.root_path, self._verdict(root).state)
        except RootBlockedError as error:
            return RootStatus(located.root_path, error.state)

    def root_state(self, root_path: str) -> RootState:
        """The sentinel's verdict on a writable physical root, `MISSING` when it is not mounted."""
        key = self._host_root(root_path).root_path
        try:
            with self._root(key) as root:
                return self._verdict(root).state
        except RootBlockedError as error:
            return error.state

    def require_scan_allowed(self, local: str) -> None:
        located = self._locate(local)
        if located.read_only:
            return
        if self._frozen:
            raise RootBlockedError(located.root_path, RootState.RESTORED_BEHIND)
        with self._root(located.root_path) as root:
            verdict = self._verdict(root)
            if verdict.blocks_scans_and_deletions:
                raise RootBlockedError(located.root_path, verdict.state)

    def require_delete_allowed(self, local: str) -> None:
        located = self._locate(local)
        if located.read_only:
            raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
        self.require_scan_allowed(local)

    def _owes(self, root_path: str) -> bool:
        owed = self._tombstones_owed
        return owed is not None and owed(root_path)

    def boot(self) -> BootReport:
        """Per root: judge the sentinel, re-stamp a database one ahead, purge orphan staging."""
        report = BootReport()
        for root_path in sorted(self._expected):
            if self._frozen:
                report.blocked[root_path] = RootState.RESTORED_BEHIND
                continue
            try:
                with self._root(root_path) as root:
                    verdict = self._verdict(root)
                    if verdict.blocks_writes:
                        report.blocked[root_path] = verdict.state
                        continue
                    if self._owes(root_path):
                        report.tombstone_owed.append(root_path)  # only the replay may write now
                        continue
                    if verdict.restamp:
                        self._stamp(root, self._expected[root_path].generation)
                        report.restamped.append(root_path)
                    ensure_host_dirs(root)
                    report.purged_staging += self._system_empty(
                        root, "staging_orphan_cleanup", STAGING_DIR
                    )
                    report.ok.append(root_path)
            except RootBlockedError as error:
                report.blocked[root_path] = error.state
            except OSError:
                # The host folder is a link, a special file or tampered with: not a root to trust.
                report.blocked[root_path] = RootState.MISMATCH
        return report

    def require_stampable(self, root_path: str) -> None:
        """Before the database moves a generation: the sentinel could follow (no freeze, no
        tombstone line owed). Raises what `stamp_sentinel` would, so nothing drifts (I39)."""
        key = clean_absolute(root_path)
        if self._frozen:
            raise RootBlockedError(key, RootState.RESTORED_BEHIND)
        if self._owes(key):
            raise TombstonePendingError(key)

    def stamp_sentinel(self, root_path: str, *, generation: int) -> None:
        """After the database commit: rewrite the sentinel with the new generation (I39)."""
        if self._frozen:
            raise RootBlockedError(clean_absolute(root_path), RootState.RESTORED_BEHIND)
        key = clean_absolute(root_path)
        expected = self._expected[key]
        if self._owes(key):
            raise TombstonePendingError(key)
        with self._root(key) as root:
            self._stamp(root, generation)
        self._expected[key] = replace(expected, generation=generation)

    def _stamp(
        self, root: ConfinedRoot, generation: int, *, restore_epoch: int | None = None
    ) -> None:
        current = read_sentinel(root)
        if current is None:
            raise RootBlockedError(root.path, RootState.MISSING)
        expected = self._expected[root.path]
        if (current.root_id, current.instance_id) != (expected.root_id, expected.instance_id):
            raise RootBlockedError(root.path, RootState.MISMATCH)  # never overwrite a foreign one
        write_sentinel(root, current.restamped(generation, restore_epoch=restore_epoch))

    def sentinel_of(self, root_path: str, *, strict: bool = False) -> SentinelDocument | None:
        """The sentinel as it is on disk, for the restore check: read only, never gated, never
        followed through a link. None when absent or unreadable (the caller treats both as
        "not trusted")."""
        key = clean_absolute(root_path)
        if key not in self._expected:
            raise OutsidePathMapError(f"{root_path!r} is not a writable physical root")
        try:
            with self._root(key) as root:
                return read_sentinel(root)
        except (OSError, SentinelFormatError):
            if strict:
                raise
            return None

    def restamp_after_restore(self, root_path: str, *, generation: int, restore_epoch: int) -> None:
        """The one write allowed while the instance is `restored_behind`: the sentinel of a root
        whose files were just re-adopted takes the database's new generation and epoch
        (`reconcile --adopt-after-restore`, EF-INT-026). Only a sentinel that is this instance's
        and this root's, and only forward (never to a lower generation or epoch than it has)."""
        key = clean_absolute(root_path)
        if key not in self._expected:
            raise OutsidePathMapError(f"{root_path!r} is not a writable physical root")
        with self._root(key) as root:
            current = read_sentinel(root)
            if current is None:
                raise RootBlockedError(key, RootState.MISSING)
            expected = self._expected[key]
            if (current.root_id, current.instance_id) != (expected.root_id, expected.instance_id):
                raise RootBlockedError(key, RootState.MISMATCH)
            if generation < current.generation or restore_epoch < current.restore_epoch:
                raise RootBlockedError(key, RootState.RESTORED_BEHIND)
            write_sentinel(root, current.restamped(generation, restore_epoch=restore_epoch))
        self._expected[key] = replace(expected, generation=generation, restore_epoch=restore_epoch)

    # -- reads ------------------------------------------------------------------------------

    def read_bytes(self, local: str) -> bytes:
        located = self._locate(local)
        with self._root(located.root_path) as root:
            fd = root.open_file(located.rel, os.O_RDONLY)
            try:
                with os.fdopen(os.dup(fd), "rb") as source:
                    return source.read()
            finally:
                os.close(fd)

    @contextmanager
    def open_read(self, local: str) -> Iterator[BinaryIO]:
        """A regular file (`open_file` refuses any other) opened for reading, as a seekable stream.

        For callers that must not hold a whole archive in memory (`safe_open_archive` on a CBZ)."""
        located = self._locate(local)
        with self._root(located.root_path) as root:
            fd = root.open_file(located.rel, os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                yield stream

    def free_bytes(self, local: str) -> int:
        """Free space, in bytes, on the file system of the physical root that holds `local`
        (what the dry-run of a copy between roots compares with its size, ENF-OPS-006)."""
        located = self._locate(local)
        stat = os.statvfs(located.root_path)
        return int(stat.f_bavail * stat.f_frsize)

    def device_of(self, local: str) -> int:
        """`st_dev` of a file (a link is not followed): with the inode, what makes a hard link."""
        located = self._locate(local)
        with self._root(located.root_path) as root:
            return root.stat(located.rel).st_dev

    def observe(self, local: str, *, host: bool = False) -> tuple[Kind, FileIdentity] | None:
        """What is at `local` (a link is described, never followed), or None when nothing is.
        `host` lets the purge look at an entry of a root's bin (a reserved folder)."""
        located = self._locate(local, read_mirror=True, host=host)
        with self._root(located.root_path) as root:
            try:
                observed = root.stat(located.rel)
            except FileNotFoundError:
                return None
            kind = root.lstat_kind(located.rel)
        return (kind, identity_of(observed)) if kind is not None else None

    def list_dir(self, local: str) -> dict[str, Kind]:
        """The entries of a folder with what each is (a link is described, never followed).
        An entry that vanishes while it is read is left out."""
        located = self._locate(local)
        with self._root(located.root_path) as root:
            found: dict[str, Kind] = {}
            for name in root.listdir(located.rel):
                kind = root.lstat_kind(f"{located.rel}/{name}" if located.rel != "." else name)
                if kind is not None:
                    found[name] = kind
            return found

    def sha256_of(self, local: str) -> str:
        """`sha256:<hex>` of a regular file, streamed."""
        located = self._locate(local, read_mirror=True)
        with self._root(located.root_path) as root:
            fd = root.open_file(located.rel, os.O_RDONLY)
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb") as stream:
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    # -- the per-root lease (ENF-OPS-015) ------------------------------------------------------

    def read_lease(self, root_path: str) -> bytes | None:
        """The raw lease of a root, or None. A read: it writes nothing and gates nothing."""
        key = clean_absolute(root_path)
        if key not in self._expected:
            raise OutsidePathMapError(f"{root_path!r} is not a writable physical root")
        try:
            with self._root(key) as root:
                return _read_lease(root)
        except RootBlockedError:
            return None

    def update_lease(
        self,
        root_path: str,
        decide: Callable[[bytes | None], bytes | Literal["remove"] | None],
    ) -> None:
        """Read the lease, decide, and write, all in the one root opened for the write.

        The sentinel and the generation counter are judged on that same open root first, so a
        lease is never taken, renewed or removed on a root that is missing, foreign or behind
        (EF-EXP-029). `decide` gets the current bytes and returns the new ones, "remove" or None
        (leave as is); whatever it raises propagates and nothing is written.
        """
        key = clean_absolute(root_path)
        if key not in self._expected:
            raise OutsidePathMapError(f"{root_path!r} is not a writable physical root")
        located = _Located(key, ".", False)
        with self._gated(located, lease=False) as root:
            current = _read_lease(root)
            outcome = decide(current)
            if outcome is None:
                return
            if current is not None:
                _claim_lease(root, current)  # compare-and-swap: only if nobody changed it since
            if outcome != "remove":
                try:  # exclusive creation: a racing taker makes this fail, not overwrite
                    write_atomic(root, LEASE_FILE, outcome, parents=True, replace=False)
                except FileExistsError:
                    raise LeaseRaceError(f"{root_path}: the lease was taken meanwhile") from None

    # -- the provenance mirror (EF-EXP-063): `<root>/.shelfhold/manifest/`, host-only ---------

    def _mirror_root(self, root_path: str) -> str:
        key = clean_absolute(root_path)
        if key in self._expected:
            return key
        if key in self._read_only:
            raise ReadOnlyRootError(f"{root_path} is an adoption root: never written")
        raise OutsidePathMapError(f"{root_path!r} is not a physical root")

    def _require_space(self) -> None:
        """Writes that add data stop under the disk guard's mark; renames and removals never are
        (the guard deletes nothing and never blocks the cure)."""
        if self._write_gate is not None:
            self._write_gate()

    def write_mirror(self, root_path: str, name: str, data: bytes) -> FileIdentity:
        """Write one mirror file (`<dest>/<series>.jsonl`) atomically: the root is judged (sentinel,
        generation, lease) like for any write, and an `adopt_ro` root is refused."""
        key = self._mirror_root(root_path)
        self._require_space()
        with self._gated(_Located(key, ".", False)) as root:
            return write_mirror_file(root, name, data)

    def read_mirror(self, root_path: str, name: str) -> bytes | None:
        with self._root(self._mirror_root(root_path)) as root:
            return read_mirror_file(root, name)

    def list_mirror(self, root_path: str) -> list[str]:
        with self._root(self._mirror_root(root_path)) as root:
            return list_mirror_files(root)

    # -- the tombstone journal and kid registry of a root (EF-BIB-053, EF-BIB-054) ---------------

    def _host_root(self, root_path: str) -> _Located:
        key = clean_absolute(root_path)
        if key not in self._expected:
            raise OutsidePathMapError(f"{root_path!r} is not a writable physical root")
        return _Located(key, ".", False)

    def append_tombstone_line(self, root_path: str, line: bytes, *, header: bytes) -> None:
        """Append one line to the root's journal: a write like any other (sentinel, lease)."""
        with self._gated(self._host_root(root_path), tombstone=True) as root:
            append_line(root, line, header=header)

    def record_tombstone_kid(self, root_path: str, kid: str, tags: dict[str, str]) -> None:
        """Add `kid` and its tags to the registry of the root's sentinel, before any line."""
        with self._gated(self._host_root(root_path), tombstone=True) as root:
            add_sentinel_kid(root, kid, tags)

    def read_tombstone_journal(self, root_path: str, *, max_bytes: int) -> bytes | None:
        """The raw journal. Not gated by the sentinel: it is replayed even when the root is
        behind or foreign (I36); what a line means is decided by its authentication."""
        with self._root(self._host_root(root_path).root_path) as root:
            return read_journal(root, max_bytes)

    def tombstone_kids(self, root_path: str) -> dict[str, dict[str, str]]:
        """The registry entries of the root's sentinel, kid -> tags (unverified: the caller
        holds the keys). Empty when the sentinel is absent or unreadable."""
        with self._root(self._host_root(root_path).root_path) as root:
            try:
                return sentinel_kids(read_sentinel(root))
            except (SentinelFormatError, OSError):
                return {}

    # -- writes -----------------------------------------------------------------------------

    def write_atomic(
        self,
        local: str,
        data: bytes | Readable,
        *,
        parents: bool = False,
        replace: bool = True,
        mtime_ns: int | None = None,
        verify_digest: Callable[[], str] | None = None,
    ) -> FileIdentity:
        """Stage, flush, (optionally) read the staged file back against `verify_digest()` (a
        hex SHA-256), then put it in place: nothing is placed when the digest does not match."""
        located = self._locate(local)
        self._require_space()
        with self._gated(located) as root:
            return write_atomic(
                root, located.rel, data, parents=parents, replace=replace, mtime_ns=mtime_ns,
                verify_digest=verify_digest,
            )  # fmt: skip

    def mkdir(self, local: str, *, parents: bool = True, exist_ok: bool = True) -> None:
        located = self._locate(local)
        self._require_space()
        with self._gated(located) as root:
            root.mkdir(located.rel, parents=parents, exist_ok=exist_ok)

    def unlink(self, local: str) -> None:
        located = self._locate(local)
        with self._gated(located) as root:
            root.unlink(located.rel)

    def purge_unlink(self, local: str) -> None:
        """`purge --now` deletes one regular file: a copy of a Unit in a read-write root, or a
        file of that root's bin (EF-BIB-052). The caller has passed the reserved-action rule, has
        recorded the tombstone and has written the intent; this is the primitive, and it adds
        what only fsops can check: the path is under a writable root, never through a link, never
        a directory, never the host's own files (sentinel, lease, tombstone journal, mirror)."""
        located = self._locate(local, host=True)
        self._require_purgeable(located.rel)
        with self._gated(located) as root:
            if root.lstat_kind(located.rel) != "file":
                raise NotRegularFileError(f"{local!r} is not a regular file")
            root.unlink(located.rel)

    def purge_remove_tree(self, local: str) -> None:
        """The same for one entry of a root's bin (`.shelfhold/recycle/<id>`) and what it holds,
        the only folder a purge removes."""
        located = self._locate(local, host=True)
        if not Perimeter(below=RECYCLE_DIR).allows(located.rel):
            raise ReservedPathError(f"{local!r} is not inside a bin")
        with self._gated(located) as root:
            if root.lstat_kind(located.rel) != "dir":
                raise NotRegularFileError(f"{local!r} is not a bin entry")
            root.remove_tree(located.rel)

    @staticmethod
    def _require_purgeable(rel: str) -> None:
        if (
            is_reserved(rel)
            and not Perimeter(below=RECYCLE_DIR).allows(rel)
            and not is_received_rel(rel)  # a received archive is purged like any copy
        ):
            raise ReservedPathError(f"{rel!r} belongs to the host: a purge never deletes it")

    def system_unlink(self, emitter: str, local: str) -> None:
        """A system emitter deletes one file or link, inside its own perimeter and only while its
        condition holds (N2). `emitter` is a string literal at every call site (an architecture
        test checks it) and is the class of the calling function.

        The perimeter is enforced here, on the path relative to its physical root, before
        anything is opened; the condition is read from the file system (the age of the trash
        entry) or from the owner of this object (the disk guard), never from the caller."""
        located = self._locate(local, host=True)
        require_inside(emitter, located.rel)
        with self._gated(located) as root:
            self._require_condition(emitter, root, located.rel, is_tree=False)
            root.unlink(located.rel)

    def system_remove_tree(self, emitter: str, local: str) -> None:
        """A system emitter deletes a folder and what it holds, same rules as `system_unlink`;
        never for the probe emitter, which removes single files only."""
        located = self._locate(local, host=True)
        require_inside(emitter, located.rel)
        require_tree_allowed(emitter)
        with self._gated(located) as root:
            self._require_condition(emitter, root, located.rel, is_tree=True)
            root.remove_tree(located.rel)

    def _require_condition(
        self, emitter: str, root: ConfinedRoot, rel: str, *, is_tree: bool
    ) -> None:
        if perimeter_of(emitter).needs_file and root.lstat_kind(rel) != "file":
            raise EmitterPerimeterError(f"{emitter} removes a regular file, not {rel!r}")
        if emitter == "trash_retention_purge":
            entry = trash_entry(rel)
            try:
                age = self._now() - root.stat(entry).st_mtime
            except FileNotFoundError:
                return  # nothing to delete: the removal below reports it
            if age < TRASH_RETENTION_S:
                raise EmitterConditionError(f"{entry} is {int(age)} s old: kept 30 days")

    def _system_empty(self, root: ConfinedRoot, emitter: str, rel: str) -> int:
        require_content_inside(emitter, rel)
        return root.empty_dir(rel)

    def rmdir(self, local: str) -> None:
        located = self._locate(local)
        with self._gated(located) as root:
            root.rmdir(located.rel)

    def rename(self, src: str, dst: str, *, replace: bool = False) -> None:
        """Within one physical root only; between two roots it would be a copy (I24).

        An existing target is an error (`FileExistsError`) unless `replace=True`."""
        a, b = self._locate(src), self._locate(dst)
        if a.read_only or b.read_only:
            raise ReadOnlyRootError("an adoption root is never written")
        if a.root_path != b.root_path:
            raise CrossRootError(f"{a.root_path} and {b.root_path} are different physical roots")
        with self._gated(a) as root:
            root.rename(a.rel, b.rel, replace=replace)

    def recycle(self, local: str) -> str:
        """Move into the same root's `.shelfhold/recycle`; returns where it went."""
        located = self._locate(local)
        with self._gated(located) as root:
            folder = f"{RECYCLE_DIR}/{uuid.uuid4().hex}"
            target = f"{folder}/{located.rel}"
            try:
                root.mkdir(target.rpartition("/")[0], parents=True, exist_ok=True, durable=False)
                root.rename(located.rel, target, replace=False)
            except BaseException:
                with contextlib.suppress(OSError):
                    root.remove_tree(folder)  # only empty folders made just above
                raise
            return f"{root.path}/{target}"

    def copy_across(
        self,
        src: str,
        dst: str,
        *,
        parents: bool = False,
        replace: bool = False,
        expected_sha256: str | None = None,
    ) -> str:
        """Copy, verify the staged copy, then put it in place; never a `rename` between roots.

        Returns the SHA-256 of what was copied. The source may be an adoption root. With
        `expected_sha256` (hex), the staged copy must have that digest: otherwise nothing is put
        in place (`CopyVerificationError`), whatever the source became since it was looked at.
        """
        a, b = self._locate(src), self._locate(dst)
        if b.read_only:
            raise ReadOnlyRootError(f"{b.root_path} is an adoption root: never written")
        self._require_space()
        with self._root(a.root_path) as source, self._gated(b) as target:
            fd = source.open_file(a.rel, os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                reader = HashingReader(stream)
                write_atomic(
                    target,
                    b.rel,
                    reader,
                    parents=parents,
                    replace=replace,
                    verify_digest=(lambda: expected_sha256)
                    if expected_sha256
                    else reader.digest.hexdigest,
                )
                return reader.digest.hexdigest()
