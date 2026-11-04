"""The one entry point for writing under /library (EF-EXP-006, EF-EXP-029, EF-EXP-059, EF-EXP-060).

Callers give absolute local paths (`/library/manga/X/1.cbz`). The path is located lexically in one
physical root, the host-only names are refused, the entry of the path map decides read-only or
not, and the root's sentinel is read again before every write. Only then is the root opened and
the operation done, through descriptors, in the same open root that the sentinel was read from.
Nothing here ever reads a path that was checked by a separate call.
"""

import contextlib
import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Self

from shelfhold.export.domain.path_map import (
    PathMap,
    PathMapError,
    PathMode,
    clean_absolute,
    within,
)
from shelfhold.export.domain.physical_roots import (
    RECYCLE_DIR,
    STAGING_DIR,
    ExpectedRoot,
    RootState,
    SentinelVerdict,
    is_reserved,
    judge_sentinel,
)
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, Strategy
from shelfhold.infrastructure.fsops.errors import (
    CrossRootError,
    OutsidePathMapError,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    RootBlockedError,
    SentinelFormatError,
)
from shelfhold.infrastructure.fsops.identity import FileIdentity
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
    ) -> None:
        self._path_map = path_map
        self._expected = {clean_absolute(root.path): root for root in expected}
        self._read_only = {e.local for e in path_map.entries if e.mode is PathMode.ADOPT_RO}
        self._umask = umask
        self._strategy = strategy

    @classmethod
    def open(
        cls,
        path_map: PathMap,
        expected: Sequence[ExpectedRoot],
        *,
        umask: int | None = None,
        strategy: Strategy = Strategy.AUTO,
    ) -> Self:
        """No file system access: roots are opened, and their sentinels read, per operation."""
        return cls(path_map, expected, umask=umask, strategy=strategy)

    # -- locating ---------------------------------------------------------------------------

    def _locate(self, local: str) -> _Located:
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
        if is_reserved(rel):
            raise ReservedPathError(f"{local!r} is reserved to the host")
        if not read_only:
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
    def _gated(self, located: _Located) -> Iterator[ConfinedRoot]:
        """The root opened for a write: refused when read-only or when its sentinel says so."""
        if located.read_only:
            raise ReadOnlyRootError(f"{located.root_path} is an adoption root: never written")
        with self._root(located.root_path) as root:
            verdict = self._verdict(root)
            if verdict.blocks_writes:
                raise RootBlockedError(located.root_path, verdict.state)
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
                    report.purged_staging += root.empty_dir(STAGING_DIR)
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

    # -- writes -----------------------------------------------------------------------------

    def write_atomic(
        self,
        local: str,
        data: bytes | Readable,
        *,
        parents: bool = False,
        replace: bool = True,
        mtime_ns: int | None = None,
    ) -> FileIdentity:
        located = self._locate(local)
        with self._gated(located) as root:
            return write_atomic(
                root, located.rel, data, parents=parents, replace=replace, mtime_ns=mtime_ns
            )

    def mkdir(self, local: str, *, parents: bool = True) -> None:
        located = self._locate(local)
        with self._gated(located) as root:
            root.mkdir(located.rel, parents=parents, exist_ok=True)

    def unlink(self, local: str) -> None:
        located = self._locate(local)
        with self._gated(located) as root:
            root.unlink(located.rel)

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
