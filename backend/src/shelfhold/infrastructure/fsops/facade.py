"""The one entry point for writing under /library (EF-EXP-006, EF-EXP-029, EF-EXP-059, EF-EXP-060).

Callers give absolute local paths (`/library/manga/X/1.cbz`). The path is located lexically in one
physical root, the host-only names are refused, the entry of the path map decides read-only or
not, and the root's sentinel is read again before every write. Only then is the root opened and
the operation done, through descriptors, in the same open root that the sentinel was read from.
Nothing here ever reads a path that was checked by a separate call.
"""

import contextlib
import errno
import hashlib
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Self

import structlog

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
    RESERVED_DIR,
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
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops.capabilities import RootCapabilities, probe_root
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
    FsopsError,
    JournalChangedError,
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
    replace_journal,
    sentinel_chain,
    sentinel_chain_written,
    sentinel_kids,
    set_aside,
    set_sentinel_chain,
)
from shelfhold.infrastructure.fsops.writer import (
    HashingReader,
    Readable,
    ensure_host_dirs,
    write_atomic,
    write_independent,
)
from shelfhold.infrastructure.observability.network_fs import filesystem_kind

__all__ = ["BootReport", "Fsops", "IndependentCopy", "RootStatus", "SamePathState"]


log = structlog.get_logger(__name__)


# The state of a root frozen alone (`FreezeState.state_of`) as the sentinel verdict it stands for.
ROOT_FREEZE_STATES: dict[str, RootState] = {
    "foreign_root": RootState.MISMATCH,
    "root_lost": RootState.MISSING,
    "store_lost": RootState.MISSING,
    "root_behind": RootState.STALE,
}


# The token of a same-path replacement: the ULID of its `upgrade` intent (EF-EXP-016).
_TOKEN = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")
# Not the staging area: the boot empties it before the intents are settled, and the staged file of
# an interrupted replacement is the new version (before the swap) or the old one (after it).
SAME_PATH_DIR = f"{RESERVED_DIR}/upgrade"  # `<root>/.shelfhold/upgrade/<token>`
OTHER = "other"  # what `SamePathState` says of an entry that is not a regular file
_NO_EXCHANGE = (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EPERM)


@dataclass(frozen=True)
class SamePathState:
    """The three places of a same-path replacement, each as `sha256:<hex>` of the regular file
    there, `"other"` for anything else, None when empty: the served path, the staged file (the
    new version before the swap, the old one after it) and the bin entry of the old version."""

    target: str | None
    staged: str | None
    recycled: str | None


def _digest_at(root: ConfinedRoot, rel: str) -> str | None:
    kind = root.lstat_kind(rel)
    if kind is None:
        return None
    if kind != "file":
        return OTHER
    fd = root.open_file(rel, os.O_RDONLY)
    try:
        return "sha256:" + _digest_of(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class IndependentCopy:
    """What `copy_independent` made: a file of its own inode, with the digest it was verified at."""

    sha256: str  # hex
    reflinked: bool  # the extents are shared (copy-on-write); the inode is still its own
    identity: FileIdentity


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
        freeze: FreezeState | None = None,
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
        self._freeze = freeze if freeze is not None else FreezeState(process=frozen)
        self._behind_hook: Callable[[], None] | None = None
        self._blocked_hook: Callable[[str, RootState], None] | None = None

    def _where(self, local: str) -> dict[str, str]:
        """What a DEBUG line says of a path: the ULID of its physical root, never the path, which
        holds a title (EF-BIB-053; a diagnostic bundle quotes these lines, ENF-LEG-009)."""
        try:
            path = clean_absolute(local)
        except PathMapError:
            return {}
        best = max(
            (
                root
                for root_path, root in self._expected.items()
                if within(path, root_path) is not None
            ),
            key=lambda root: len(root.path),
            default=None,
        )
        return {"root_id": best.root_id} if best is not None else {}

    @property
    def frozen(self) -> bool:
        return self._freeze.frozen

    @property
    def freeze_state(self) -> FreezeState:
        return self._freeze

    def _frozen_for(self, root_path: str) -> bool:
        """The freeze, for the root at `root_path`: the instance level or the root's own."""
        expected = self._expected.get(root_path)
        if expected is None:
            return self._freeze.frozen
        if not self._freeze.frozen_for(expected.root_id):
            return False
        if self._freeze.frozen or self._freeze.state_of(expected.root_id) != "root_lost":
            return True  # only a vanished sentinel comes back by itself: the rest needs its exit
        return not self._root_is_right_again(root_path, expected.root_id)

    def _root_is_right_again(self, root_path: str, root_id: str) -> bool:
        """`root_lost` only: the mark outlives a volume that came back by itself (remounted,
        sentinel put back by the owner), so the sentinel is read again and a root that is right
        releases it. `foreign_root`, `root_behind` and `store_lost` are never released by what the
        disk says now (a current sentinel copied onto an old copy would lift them): they wait for
        their reserved exit command (EF-EXP-064)."""
        try:
            with self._root(root_path) as root:
                right = not self._verdict(root).blocks_writes
        except (FsopsError, OSError):
            return False
        if right:
            self._freeze.follow_root(root_id, None)
            hook = self._blocked_hook
            if hook is not None:
                try:
                    hook(root_path, RootState.OK)  # the record of the state is refreshed
                except Exception:
                    log.warning("fsops.blocked_hook_failed", root=root_path)
        return right

    def _freeze_state_of(self, root_path: str) -> RootState:
        """Why `_frozen_for` is true: the instance (`restored_behind`) or the root alone."""
        if self._freeze.frozen:
            return RootState.RESTORED_BEHIND
        expected = self._expected.get(root_path)
        named = self._freeze.state_of(expected.root_id) if expected else None
        return ROOT_FREEZE_STATES.get(named or "", RootState.MISMATCH)

    def on_root_blocked(self, hook: Callable[[str, RootState], None] | None) -> None:
        """Called when a write finds a root whose sentinel is missing, another's or behind the
        database: the composition root records the state of that root (`foreign_root`,
        `root_lost`, `root_behind`, `store_lost`, EF-EXP-064). The hook never raises into a
        write."""
        self._blocked_hook = hook

    def on_restored_behind(self, hook: Callable[[], None] | None) -> None:
        """Called when a write finds a root whose sentinel is ahead of the database (a root that
        came back after the start): the composition root persists the freeze (EF-INT-026)."""
        self._behind_hook = hook

    def freeze(self) -> None:
        """Read-only for the whole instance, every root, until `unfreeze`: the database is older
        than the disk (`restored_behind`, EF-INT-026). Reads, observation and hashing still work;
        no write, deletion, session or scan does."""
        self._freeze.freeze()

    def unfreeze(self) -> None:
        self._freeze.thaw()

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
        freeze: FreezeState | None = None,
    ) -> Self:
        """No file system access: roots are opened, and their sentinels read, per operation.
        `frozen`: the database says `restored_behind` (read from `LoadedRoots.restore_pending`)."""
        return cls(
            path_map, expected, umask=umask, strategy=strategy,
            write_gate=write_gate, stop_gate=stop_gate, now=now, frozen=frozen,
            freeze=freeze,
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
        (`tombstone=True`: the write that pays them, still under the lease).
        After the shutdown's checkpoint delay every write is refused (`stop_gate`)."""
        entered = False
        try:
            if self._stop_gate is not None:
                self._stop_gate()
            if located.read_only:
                raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
            if self._frozen_for(located.root_path):
                raise RootBlockedError(located.root_path, self._freeze_state_of(located.root_path))
            owed = self._tombstones_owed
            if lease and not tombstone and owed is not None and owed(located.root_path):
                raise TombstonePendingError(located.root_path)
            with self._root(located.root_path) as root:
                verdict = self._verdict(root)
                if verdict.blocks_writes:
                    if verdict.state is RootState.RESTORED_BEHIND and self._behind_hook is not None:
                        self._behind_hook()
                    self._tell_blocked(located.root_path, verdict.state)
                    raise RootBlockedError(located.root_path, verdict.state)
                if lease and self._lease_verifier is not None:
                    self._lease_verifier(located.root_path, _read_lease(root))
                entered = True
                yield root
        except FsopsError as error:
            if not entered:  # a guard refused the write (the caller's own failures pass as is)
                log.warning("fsops.write_refused", reason=type(error).__name__,
                            root=located.root_path)  # fmt: skip
            raise

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
        """The sentinel's word before a scan (EF-EXP-029). A dedicated `adopt_ro` mount is never
        written, so it carries no sentinel (EF-EXP-060): its word is that the mount is there. An
        `adopt_ro` entry nested in a writable root is under that root's sentinel."""
        located = self._locate(local)
        if located.read_only and located.root_path in self._read_only:
            try:
                with self._root(located.root_path):
                    return
            except OSError as error:  # the volume is not there: Kavita would read an empty folder
                raise RootBlockedError(located.root_path, RootState.MISSING) from error
        if self._frozen_for(located.root_path):
            raise RootBlockedError(located.root_path, self._freeze_state_of(located.root_path))
        with self._root(located.root_path) as root:
            verdict = self._verdict(root)
            if verdict.blocks_scans_and_deletions:
                self._tell_blocked(located.root_path, verdict.state)
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
            if self._frozen_for(root_path):
                report.blocked[root_path] = self._freeze_state_of(root_path)
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
        if self._frozen_for(key):
            raise RootBlockedError(key, self._freeze_state_of(key))
        if self._owes(key):
            raise TombstonePendingError(key)

    def stamp_sentinel(self, root_path: str, *, generation: int) -> None:
        """After the database commit: rewrite the sentinel with the new generation (I39)."""
        if self._frozen_for(clean_absolute(root_path)):
            raise RootBlockedError(
                clean_absolute(root_path), self._freeze_state_of(clean_absolute(root_path))
            )
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

    def root_frozen(self, root_path: str) -> RootState | None:
        """Why this root alone is frozen (`foreign_root`, `root_lost`, `root_behind`,
        `store_lost`), None when it is not. The instance level is `frozen`."""
        key = clean_absolute(root_path)
        expected = self._expected.get(key)
        if expected is None or self._freeze.state_of(expected.root_id) is None:
            return None
        return self._freeze_state_of(key)

    def _tell_blocked(self, root_path: str, state: RootState) -> None:
        hook = self._blocked_hook
        if hook is None or state is RootState.OK:
            return
        try:
            hook(root_path, state)
        except Exception:  # a record of the state must never turn a refusal into another error
            log.warning("fsops.blocked_hook_failed", root=root_path)

    def takeover_sentinel(
        self,
        root_path: str,
        build: Callable[[SentinelDocument | None, bool], SentinelDocument],
    ) -> SentinelDocument:
        """The one write that stamps a root whose sentinel is not ours (`root takeover|reinit`,
        `store reinit`, EF-EXP-064). `build(found, damaged)` makes the document from what is
        there (None: nothing; `damaged`: a file that could not be read, replaced as it is) and
        decides the ids: the caller owns that rule, this method only writes. It never deletes:
        the sentinel is replaced atomically, `.shelfhold/` is created when missing and nothing
        under it is touched. A sentinel of a newer major format is never rewritten. The freeze of
        the root is the very thing this leaves; the stop barrier still applies."""
        key = clean_absolute(root_path)
        if self._stop_gate is not None:
            self._stop_gate()
        if key in self._read_only:
            raise ReadOnlyRootError(f"{key} is an adoption root: never written")
        with self._root(key) as root:
            damaged = False
            try:
                found = read_sentinel(root)
            except SentinelFormatError:
                found, damaged = None, True
            if found is not None and found.read_only:
                raise SentinelFormatError(
                    f"sentinel is format {found.schema_version}, newer than this build: read only"
                )
            doc = build(found, damaged)
            write_sentinel(root, doc)
            ensure_host_dirs(root)
        expected = self._expected.get(key)
        if expected is not None:
            self._expected[key] = replace(
                expected, root_id=doc.root_id, instance_id=doc.instance_id,
                generation=doc.generation, restore_epoch=doc.restore_epoch,
            )  # fmt: skip
        return doc

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

    def link_stat(self, local: str) -> tuple[int, int, int, int, int]:
        """`(st_dev, st_ino, st_nlink, st_mtime_ns, st_ctime_ns)` of what is at `local`, a link
        not followed. Read from the disk each time: a caller that must know whether two names are
        one file, or whether a file is the one it saw, asks this, never the registry
        (EF-BIB-038). An inode number alone proves nothing: a freed one is handed out again."""
        located = self._locate(local, read_mirror=True)
        with self._root(located.root_path) as root:
            found = root.stat(located.rel)
        return found.st_dev, found.st_ino, found.st_nlink, found.st_mtime_ns, found.st_ctime_ns

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

    def list_dir(self, local: str, *, host: bool = False) -> dict[str, Kind]:
        """The entries of a folder with what each is (a link is described, never followed).
        An entry that vanishes while it is read is left out. `host`: the reserved area (a bin)."""
        located = self._locate(local, host=host)
        with self._root(located.root_path) as root:
            found: dict[str, Kind] = {}
            for name in root.listdir(located.rel):
                kind = root.lstat_kind(f"{located.rel}/{name}" if located.rel != "." else name)
                if kind is not None:
                    found[name] = kind
            return found

    def is_read_only(self, local: str) -> bool:
        """`local` is under an adoption root (`ADOPT_RO`): nothing is written or moved there."""
        return self._locate(local).read_only

    def sha256_of(self, local: str, *, host: bool = False) -> str:
        """`sha256:<hex>` of a regular file, streamed. `host`: the reserved area (a bin)."""
        located = self._locate(local, read_mirror=True, host=host)
        with self._root(located.root_path) as root:
            fd = root.open_file(located.rel, os.O_RDONLY)
            digest = hashlib.sha256()
            with os.fdopen(fd, "rb") as stream:
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    def hash_and_identity(self, local: str) -> tuple[str, FileIdentity, int] | None:
        """`(sha256, identity, st_dev)` of a regular file, all from the one descriptor that was
        opened: the digest and the identity describe the same bytes (no swap in between). None
        when it is not a regular file."""
        located = self._locate(local, read_mirror=True)
        with self._root(located.root_path) as root:
            try:
                fd = root.open_file(located.rel, os.O_RDONLY)
            except (FileNotFoundError, NotADirectoryError):
                return None
            with os.fdopen(fd, "rb") as stream:
                observed = os.fstat(stream.fileno())
                if not stat.S_ISREG(observed.st_mode):
                    return None
                digest = hashlib.sha256()
                while chunk := stream.read(1 << 20):
                    digest.update(chunk)
        return "sha256:" + digest.hexdigest(), identity_of(observed), observed.st_dev

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

    def append_tombstone_line(self, root_path: str, line: bytes, *, header: bytes) -> bool:
        """Append one line to the root's journal: a write like any other (sentinel, lease).
        True when this line created the file."""
        with self._gated(self._host_root(root_path), tombstone=True) as root:
            return append_line(root, line, header=header)

    def replace_tombstone_journal(self, root_path: str, new: bytes, expected: bytes) -> bool:
        """`tombstone rekey`: `new` takes the place of the journal, which must still be
        `expected`; False, nothing replaced, when it is not (written meanwhile). A write like any
        other (sentinel, lease); the only one that rewrites the journal."""
        with self._gated(self._host_root(root_path), tombstone=True) as root:
            try:
                replace_journal(root, new, expected)
            except JournalChangedError:
                return False
        return True

    def record_tombstone_chain(self, root_path: str, chain: dict[str, Any]) -> bool:
        """The chained label of the journal, copied into the root's sentinel (authenticated)."""
        with self._gated(self._host_root(root_path), tombstone=True) as root:
            return set_sentinel_chain(root, chain)

    def tombstone_chain(self, root_path: str) -> dict[str, Any] | None:
        """The `tombstone_chain` field of the root's sentinel, unverified (the caller holds the
        keys); None when the sentinel is absent, unreadable or has none."""
        with self._root(self._host_root(root_path).root_path) as root:
            try:
                return sentinel_chain(read_sentinel(root))
            except (SentinelFormatError, OSError):
                return None

    def tombstone_chain_written(self, root_path: str) -> bool:
        """True when the root's sentinel has carried a chain (field or minor version): without a
        valid one it is then suspect, not new."""
        with self._root(self._host_root(root_path).root_path) as root:
            try:
                return sentinel_chain_written(read_sentinel(root))
            except (SentinelFormatError, OSError):
                return False

    def record_tombstone_kid(self, root_path: str, kid: str, tags: dict[str, str]) -> None:
        """Add `kid` and its tags to the registry of the root's sentinel, before any line."""
        with self._gated(self._host_root(root_path), tombstone=True) as root:
            add_sentinel_kid(root, kid, tags)

    def read_tombstone_journal(self, root_path: str, *, max_bytes: int) -> bytes | None:
        """The raw journal. Not gated by the sentinel: it is replayed even when the root is
        behind or foreign (I36); what a line means is decided by its authentication."""
        with self._root(self._host_root(root_path).root_path) as root:
            return read_journal(root, max_bytes)

    def set_aside_tombstone_journal(self, root_path: str, stamp: str) -> str | None:
        """Rename an unreadable journal to `tombstones.aside-<stamp>` (nothing is deleted). Allowed
        while the instance is `restored_behind` (it is how a restore leaves that state when a
        journal is damaged), but only for this instance's own root. The new name, or None."""
        if self._stop_gate is not None:
            self._stop_gate()
        key = self._host_root(root_path).root_path
        with self._root(key) as root:
            current = read_sentinel(root)
            expected = self._expected[key]
            if current is None:
                raise RootBlockedError(key, RootState.MISSING)
            if (current.root_id, current.instance_id) != (expected.root_id, expected.instance_id):
                raise RootBlockedError(key, RootState.MISMATCH)
            return set_aside(root, stamp)

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
        log.debug("fsops.write_atomic", **self._where(local))
        located = self._locate(local)
        self._require_space()
        with self._gated(located) as root:
            return write_atomic(
                root, located.rel, data, parents=parents, replace=replace, mtime_ns=mtime_ns,
                verify_digest=verify_digest,
            )  # fmt: skip

    def probe_capabilities(
        self, root_path: str, *, store_path: str | None = None
    ) -> RootCapabilities:
        """What the root can do (reflink, hard link to `/store`, stable inodes), probed under the
        lease and removed before returning (EF-EXP-059). `/store` takes part only when it is a
        mounted, writable physical root of this instance; otherwise the link is "not applicable"
        and the reason says why (M1a has no `/store`)."""
        located = self._host_root(root_path)
        store, reason = None, "no /store in this instance"
        if store_path is not None and Path(store_path).is_dir():
            try:
                store = self._host_root(store_path)
            except (OutsidePathMapError, PathMapError):
                reason = "/store is not a physical root of this instance"
        if store is not None and store.root_path == located.root_path:
            store, reason = None, "this root is /store"
        stamp = datetime.fromtimestamp(self._now(), UTC).isoformat()
        kind = filesystem_kind(Path(located.root_path))
        self._require_space()  # the disk guard stops the probe like any other write

        def remove(which: str, rel: str) -> None:
            owner = store.root_path if which == "store" and store is not None else located.root_path
            self.system_unlink("probe_file_cleanup", f"{owner}/{rel}")

        with self._gated(located) as root:  # the lease, and the lines owed to the root
            if store is None:
                return probe_root(
                    root, store=None, filesystem=kind, now=stamp, remove=remove,
                    store_reason=reason,
                )  # fmt: skip
            try:
                with self._gated(store) as other:
                    return probe_root(
                        root, store=other, filesystem=kind, now=stamp, remove=remove
                    )  # fmt: skip
            except (RootBlockedError, ReadOnlyRootError, LeaseRaceError, TombstonePendingError):
                return probe_root(
                    root, store=None, filesystem=kind, now=stamp, remove=remove,
                    store_reason="/store is not writable now",
                )  # fmt: skip

    def mkdir(self, local: str, *, parents: bool = True, exist_ok: bool = True) -> None:
        log.debug("fsops.mkdir", **self._where(local))
        located = self._locate(local)
        self._require_space()
        with self._gated(located) as root:
            root.mkdir(located.rel, parents=parents, exist_ok=exist_ok)

    def unlink(self, local: str) -> None:
        log.debug("fsops.unlink", **self._where(local))
        located = self._locate(local)
        with self._gated(located) as root:
            root.unlink(located.rel)

    def purge_unlink(self, local: str) -> None:
        """`purge --now` deletes one regular file: a copy of a Unit in a read-write root, or a
        file of that root's bin (EF-BIB-052). The caller has passed the reserved-action rule, has
        recorded the tombstone and has written the intent; this is the primitive, and it adds
        what only fsops can check: the path is under a writable root, never through a link, never
        a directory, never the host's own files (sentinel, lease, tombstone journal, mirror)."""
        log.debug("fsops.purge_unlink", **self._where(local))
        located = self._locate(local, host=True)
        self._require_purgeable(located.rel)
        with self._gated(located) as root:
            if root.lstat_kind(located.rel) != "file":
                raise NotRegularFileError(f"{local!r} is not a regular file")
            root.unlink(located.rel)

    def purge_remove_tree(self, local: str) -> None:
        """The same for one entry of a root's bin (`.shelfhold/recycle/<id>`) and what it holds,
        the only folder a purge removes."""
        log.debug("fsops.purge_remove_tree", **self._where(local))
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
        log.debug("fsops.system_unlink", **self._where(local))
        located = self._locate(local, host=True)
        require_inside(emitter, located.rel)
        with self._gated(located) as root:
            self._require_condition(emitter, root, located.rel, is_tree=False)
            root.unlink(located.rel)

    def trash_expired(self, local: str) -> bool:
        """The bin entry holding `local` is old enough for `trash_retention_purge` (30 days). A
        question, not a deletion: the sweep asks before it records an intention."""
        located = self._locate(local, host=True)
        require_inside("trash_retention_purge", located.rel)
        with self._root(located.root_path) as root:
            try:
                return bool(
                    self._now() - root.stat(trash_entry(located.rel)).st_mtime >= TRASH_RETENTION_S
                )
            except FileNotFoundError:
                return False

    def system_remove_tree(self, emitter: str, local: str) -> None:
        """A system emitter deletes a folder and what it holds, same rules as `system_unlink`;
        never for the probe emitter, which removes single files only."""
        log.debug("fsops.system_remove_tree", **self._where(local))
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
        log.debug("fsops.rmdir", **self._where(local))
        located = self._locate(local)
        with self._gated(located) as root:
            root.rmdir(located.rel)

    def rename(self, src: str, dst: str, *, replace: bool = False) -> None:
        """Within one physical root only; between two roots it would be a copy (I24).

        An existing target is an error (`FileExistsError`) unless `replace=True`."""
        log.debug(
            "fsops.rename",
            **self._where(src),
            dst_root_id=self._where(dst).get("root_id"),
            replace=replace,
        )
        a, b = self._locate(src), self._locate(dst)
        if a.read_only or b.read_only:
            raise ReadOnlyRootError("an adoption root is never written")
        if a.root_path != b.root_path:
            raise CrossRootError(f"{a.root_path} and {b.root_path} are different physical roots")
        with self._gated(a) as root:
            root.rename(a.rel, b.rel, replace=replace)

    def relocate_inbox(
        self, root_path: str, old_rel: str, new_rel: str
    ) -> tuple[list[str], list[str]]:
        """Move the regular files of the folder `old_rel` of a writable root into `new_rel` (both
        relative to the root), then remove `old_rel` if it is empty. One renaming per file,
        inside the root, never replacing. Returns `(moved, left)`: the names moved, and the
        entries left where they were (a name already taken, a sub-folder, a link, or an
        `old_rel` that is itself a link or not a folder). The one-time move of the follow inbox
        into the host area (`.received` -> `.shelfhold/received`)."""
        with self._gated(self._host_root(root_path)) as root:
            kind = root.lstat_kind(old_rel)
            if kind is None:
                return [], []
            if kind != "dir":
                return [], [old_rel]
            root.mkdir(new_rel, parents=True, exist_ok=True)
            moved: list[str] = []
            left: list[str] = []
            for name in sorted(root.listdir(old_rel)):
                if root.lstat_kind(f"{old_rel}/{name}") != "file":
                    left.append(name)
                    continue
                try:
                    root.rename(f"{old_rel}/{name}", f"{new_rel}/{name}", replace=False)
                except FileExistsError:
                    left.append(name)
                    continue
                moved.append(name)
            if not root.listdir(old_rel):
                root.rmdir(old_rel)
            return moved, left

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

    # -- same-path replacement (EF-EXP-016) -------------------------------------------------
    #
    # Three steps, each under the `upgrade` intent whose identifier is `token`, so that a crash
    # between any two leaves a state the replay knows (`IntentJournal`): stage the new version in
    # a host folder of the root, swap it with the served file in one `RENAME_EXCHANGE`, move the old
    # one (now staged) into the root's bin. At every instant the path holds exactly one file, and
    # the staged file and the bin are in host folders that no library scans. Without
    # `RENAME_EXCHANGE` the old file is first copied into the bin (an independent, verified copy),
    # then the staged one replaces it in one `rename`: the same guarantee, one copy more.

    def _same_path(self, target: str, token: str) -> tuple[_Located, str, str]:
        if not _TOKEN.fullmatch(token):
            raise ValueError("a same-path replacement is named by the ULID of its intent")
        located = self._locate(target)
        if located.read_only:
            raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
        if located.rel == ".":
            raise NotRegularFileError("the root itself is not a file")
        staged = f"{SAME_PATH_DIR}/{token}"
        return located, staged, f"{RECYCLE_DIR}/{token}/{located.rel}"

    def same_path_bin_entry(self, target: str, token: str) -> tuple[str, str]:
        """`(local path, path relative to the bin)` where the old version of `target` goes."""
        located, _staged, recycled = self._same_path(target, token)
        return f"{located.root_path}/{recycled}", recycled.removeprefix(f"{RECYCLE_DIR}/")

    def same_path_state(self, target: str, token: str) -> SamePathState:
        located, staged, recycled = self._same_path(target, token)
        with self._root(located.root_path) as root:
            return SamePathState(
                _digest_at(root, located.rel), _digest_at(root, staged), _digest_at(root, recycled)
            )

    def same_path_stage(
        self, target: str, token: str, data: bytes | Readable, *, verify_digest: Callable[[], str]
    ) -> FileIdentity:
        """Step 1: the new version, flushed and read back against `verify_digest()` (hex), put
        in the host folder of the target's root under the token. Absent or whole, never half."""
        log.debug("fsops.same_path_stage", **self._where(target))
        located, staged, _recycled = self._same_path(target, token)
        self._require_space()
        with self._gated(located) as root:
            if root.lstat_kind(located.rel) != "file":
                raise NotRegularFileError(f"{located.rel!r} is not a served file")
            return write_atomic(
                root, staged, data, parents=True, replace=False, verify_digest=verify_digest,
                file_mode=0o666,  # a library file (umask applies), not a host-only one
            )  # fmt: skip

    def same_path_swap(self, target: str, token: str) -> None:
        """Step 2: the staged version takes the path in one step; the old one is then staged
        (`RENAME_EXCHANGE`) or already in the bin (copy, then the replacing `rename`)."""
        log.debug("fsops.same_path_swap", **self._where(target))
        located, staged, recycled = self._same_path(target, token)
        with self._gated(located) as root:
            for rel in (staged, located.rel):
                if root.lstat_kind(rel) != "file":
                    raise NotRegularFileError(f"{rel!r} is not a regular file")
            if root.lstat_kind(recycled) is None:
                try:
                    root.exchange(staged, located.rel)
                    return
                except OSError as error:
                    if error.errno not in _NO_EXCHANGE:
                        raise
                    log.info("fsops.exchange_unsupported", root=located.root_path)
                self._copy_to_bin(root, located.rel, recycled)
            root.rename(staged, located.rel, replace=True)

    def _copy_to_bin(self, root: ConfinedRoot, rel: str, recycled: str) -> None:
        self._require_space()
        fd = root.open_file(rel, os.O_RDONLY)
        try:
            expected = _digest_of(fd)
            write_independent(
                root, recycled, fd, parents=True, replace=False, verify_digest=lambda: expected
            )
        finally:
            os.close(fd)

    def same_path_recycle_old(self, target: str, token: str) -> str:
        """Step 3: the old version, staged by the swap, into the root's bin under the token
        (never replacing). Returns where it is; a no-op when it is there already."""
        log.debug("fsops.same_path_recycle_old", **self._where(target))
        located, staged, recycled = self._same_path(target, token)
        with self._gated(located) as root:
            if root.lstat_kind(staged) is not None:
                root.mkdir(recycled.rpartition("/")[0], parents=True, exist_ok=True)
                root.rename(staged, recycled, replace=False)
            return f"{root.path}/{recycled}"

    def same_path_discard(self, target: str, token: str) -> None:
        """A replacement given up before its swap: the staged new version goes, nothing else
        (the path is the token's own)."""
        located, staged, _recycled = self._same_path(target, token)
        with self._gated(located) as root, contextlib.suppress(FileNotFoundError):
            root.unlink(staged)

    @contextmanager
    def open_bin_entry(self, target: str, path: str) -> Iterator[BinaryIO]:
        """The old version a same-path replacement put in the bin, opened for reading (the
        restore reads it back). `path` is the one recorded in the bin index, relative to the bin
        of `target`'s root (`<token>/<rel at the time>`), never recomputed: the file may have
        been renamed since. Read through the confined root (no `..`, no symbolic link)."""
        located = self._locate(target)
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise OutsidePathMapError(f"{path!r} is not a path inside a bin")
        with self._root(located.root_path) as root:
            fd = root.open_file(f"{RECYCLE_DIR}/{path}", os.O_RDONLY)
            with os.fdopen(fd, "rb") as stream:
                yield stream

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

    def copy_independent(
        self,
        src: str,
        dst: str,
        *,
        parents: bool = False,
        replace: bool = False,
        expected_sha256: str | None = None,
    ) -> IndependentCopy:
        """A copy that shares no inode with its source (EF-BIB-027, D9): a reflink clone when the
        file system allows it, else a copy flushed to disk; read back and compared to
        `expected_sha256` (hex) when given, else to the digest of the source, before it is put in
        place. Never a hard link, whatever the capabilities of the roots say.

        The caller records the intent (`copy_cross_root`) around the call, as the repatriation
        does: a crash leaves the target absent or complete, and the intent is replayed from that."""
        a, b = self._locate(src), self._locate(dst)
        if b.read_only:
            raise ReadOnlyRootError(f"{b.root_path} is an adoption root: never written")
        self._require_space()
        with self._root(a.root_path) as source, self._gated(b) as target:
            fd = source.open_file(a.rel, os.O_RDONLY)
            try:
                expected = expected_sha256 or _digest_of(fd)
                identity, cloned = write_independent(
                    target, b.rel, fd, parents=parents, replace=replace,
                    verify_digest=lambda: expected,
                )  # fmt: skip
            finally:
                os.close(fd)
        return IndependentCopy(sha256=expected, reflinked=cloned, identity=identity)


def _digest_of(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, 1 << 20, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()
