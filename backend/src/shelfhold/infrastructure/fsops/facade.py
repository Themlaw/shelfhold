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
    SentinelVerdict,
    is_reserved,
    judge_sentinel,
)
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, Kind, Strategy
from shelfhold.infrastructure.fsops.emitters import (
    TRASH_RETENTION_S,
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
    OutsidePathMapError,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
    SentinelFormatError,
)
from shelfhold.infrastructure.fsops.identity import FileIdentity, identity_of
from shelfhold.infrastructure.fsops.sentinel import read_sentinel, write_sentinel
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
        disk_guard: Callable[[], bool] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._disk_guard = disk_guard
        self._now = now
        self._path_map = path_map
        self._expected = {clean_absolute(root.path): root for root in expected}
        self._read_only = {e.local for e in path_map.entries if e.mode is PathMode.ADOPT_RO}
        self._umask = umask
        self._strategy = strategy
        self._lease_verifier: Callable[[str, bytes | None], None] | None = None

    def enforce_lease(self, verify: Callable[[str, bytes | None], None] | None) -> None:
        """From now on every write passes `verify(root_path, raw_lease)`, which raises when the
        root's lease is not this instance's valid one (ENF-OPS-015). Set once at start-up, so no
        write path (journal, replay, sessions) can skip the check."""
        self._lease_verifier = verify

    @classmethod
    def open(
        cls,
        path_map: PathMap,
        expected: Sequence[ExpectedRoot],
        *,
        umask: int | None = None,
        strategy: Strategy = Strategy.AUTO,
        disk_guard: Callable[[], bool] | None = None,
        now: Callable[[], float] = time.time,
    ) -> Self:
        """No file system access: roots are opened, and their sentinels read, per operation."""
        return cls(
            path_map, expected, umask=umask, strategy=strategy, disk_guard=disk_guard, now=now
        )

    # -- locating ---------------------------------------------------------------------------

    def _locate(self, local: str, *, host: bool = False) -> _Located:
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
        if is_reserved(rel) and not host:
            raise ReservedPathError(f"{local!r} is reserved to the host")
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
    def _gated(self, located: _Located, *, lease: bool = True) -> Iterator[ConfinedRoot]:
        """The root opened for a write: refused when read-only, when its sentinel says so or when
        the lease is not ours (once `enforce_lease` is on)."""
        if located.read_only:
            raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
        with self._root(located.root_path) as root:
            verdict = self._verdict(root)
            if verdict.blocks_writes:
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

    def require_scan_allowed(self, local: str) -> None:
        located = self._locate(local)
        if located.read_only:
            return
        with self._root(located.root_path) as root:
            verdict = self._verdict(root)
            if verdict.blocks_scans_and_deletions:
                raise RootBlockedError(located.root_path, verdict.state)

    def require_delete_allowed(self, local: str) -> None:
        located = self._locate(local)
        if located.read_only:
            raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
        self.require_scan_allowed(local)

    def boot(self) -> BootReport:
        """Per root: judge the sentinel, re-stamp a database one ahead, purge orphan staging."""
        report = BootReport()
        for root_path in sorted(self._expected):
            try:
                with self._root(root_path) as root:
                    verdict = self._verdict(root)
                    if verdict.blocks_writes:
                        report.blocked[root_path] = verdict.state
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

    def stamp_sentinel(self, root_path: str, *, generation: int) -> None:
        """After the database commit: rewrite the sentinel with the new generation (I39)."""
        key = clean_absolute(root_path)
        expected = self._expected[key]
        with self._root(key) as root:
            self._stamp(root, generation)
        self._expected[key] = replace(expected, generation=generation)

    def _stamp(self, root: ConfinedRoot, generation: int) -> None:
        current = read_sentinel(root)
        if current is None:
            raise RootBlockedError(root.path, RootState.MISSING)
        expected = self._expected[root.path]
        if (current.root_id, current.instance_id) != (expected.root_id, expected.instance_id):
            raise RootBlockedError(root.path, RootState.MISMATCH)  # never overwrite a foreign one
        write_sentinel(root, current.restamped(generation))

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

    def device_of(self, local: str) -> int:
        """`st_dev` of a file (a link is not followed): with the inode, what makes a hard link."""
        located = self._locate(local)
        with self._root(located.root_path) as root:
            return root.stat(located.rel).st_dev

    def observe(self, local: str) -> tuple[Kind, FileIdentity] | None:
        """What is at `local` (a link is described, never followed), or None when nothing is."""
        located = self._locate(local)
        with self._root(located.root_path) as root:
            try:
                observed = root.stat(located.rel)
            except FileNotFoundError:
                return None
            kind = root.lstat_kind(located.rel)
        return (kind, identity_of(observed)) if kind is not None else None

    def sha256_of(self, local: str) -> str:
        """`sha256:<hex>` of a regular file, streamed."""
        located = self._locate(local)
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
        with self._gated(located) as root:
            return write_atomic(
                root, located.rel, data, parents=parents, replace=replace, mtime_ns=mtime_ns,
                verify_digest=verify_digest,
            )  # fmt: skip

    def mkdir(self, local: str, *, parents: bool = True) -> None:
        located = self._locate(local)
        with self._gated(located) as root:
            root.mkdir(located.rel, parents=parents, exist_ok=True)

    def unlink(self, local: str) -> None:
        located = self._locate(local)
        with self._gated(located) as root:
            root.unlink(located.rel)

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
        elif emitter == "disk_guard_purge" and not (self._disk_guard and self._disk_guard()):
            raise EmitterConditionError("the disk guard is not triggered")

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
        self, src: str, dst: str, *, parents: bool = False, replace: bool = False
    ) -> str:
        """Copy, verify the staged copy, then put it in place; never a `rename` between roots.

        Returns the SHA-256 of what was copied. The source may be an adoption root.
        """
        a, b = self._locate(src), self._locate(dst)
        if b.read_only:
            raise ReadOnlyRootError(f"{b.root_path} is an adoption root: never written")
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
                    verify_digest=reader.digest.hexdigest,
                )
                return reader.digest.hexdigest()
