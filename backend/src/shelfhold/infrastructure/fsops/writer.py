"""Atomic writes inside one root: staging on the same mount, fsync, rename, fsync of the folder.

The file is built under `<root>/.shelfhold/staging/<uuid>`, which is on the same file system as
its destination (no `EXDEV`), flushed, then renamed to its place relative to descriptors, and the
destination folder is flushed. A reader (Kavita) sees the old file or the new one, never half of
one (EF-EXP-006). No `chown`: the files belong to the container's user, and the umask applies.
"""

import contextlib
import errno
import fcntl
import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from shelfhold.export.domain.physical_roots import RECYCLE_DIR, STAGING_DIR
from shelfhold.infrastructure.fsops.capabilities import FICLONE, NOT_SUPPORTED
from shelfhold.infrastructure.fsops.confined import HOST_AREA, ConfinedRoot, split_rel
from shelfhold.infrastructure.fsops.emitters import require_inside
from shelfhold.infrastructure.fsops.errors import (
    CopyVerificationError,
    LinkUnavailableError,
    PathEscapeError,
)
from shelfhold.infrastructure.fsops.identity import FileIdentity, identity_of

CHUNK = 1 << 20

__all__ = [
    "HashingReader",
    "LinkedFile",
    "Readable",
    "device_of_root",
    "ensure_host_dirs",
    "link_replaceable",
    "put_file",
    "verify_copy",
    "write_atomic",
    "write_independent",
]


def ensure_host_dirs(root: ConfinedRoot) -> None:
    """Create the root's own staging and recycle folders (host area, not durable)."""
    for rel in (STAGING_DIR, RECYCLE_DIR):
        root.mkdir(rel, parents=True, exist_ok=True, durable=False)


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class HashingReader:
    """A read-only file object that hashes what passes through it."""

    def __init__(self, source: Readable) -> None:
        self._source = source
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        self.digest.update(chunk)
        return chunk


def verify_copy(fd: int, expected_sha256: str) -> bool:
    """Read the staged file back from the start and compare its digest."""
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(fd, CHUNK, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest() == expected_sha256


def write_atomic(
    root: ConfinedRoot,
    rel: str,
    data: bytes | Readable,
    *,
    parents: bool = False,
    replace: bool = True,
    mtime_ns: int | None = None,
    verify_digest: Callable[[], str] | None = None,
    file_mode: int | None = None,
) -> FileIdentity:
    identity, _cloned = _write(
        root, rel, data, None, parents=parents, replace=replace, mtime_ns=mtime_ns,
        verify_digest=verify_digest, file_mode=file_mode,
    )  # fmt: skip
    return identity


def write_independent(
    root: ConfinedRoot,
    rel: str,
    source_fd: int,
    *,
    parents: bool = False,
    replace: bool = False,
    verify_digest: Callable[[], str],
) -> tuple[FileIdentity, bool]:
    """An independent copy of the file behind `source_fd` (EF-BIB-027): a reflink clone
    (`FICLONE`) when the file system shares extents, else the bytes copied. Either way a new inode
    that is flushed and read back against `verify_digest()` before it is put in place; never a
    hard link. Returns the identity of the copy and whether it is a clone."""
    reader = os.fdopen(os.dup(source_fd), "rb", closefd=True)
    with reader:
        return _write(
            root, rel, reader, source_fd, parents=parents, replace=replace, mtime_ns=None,
            verify_digest=verify_digest, file_mode=None,
        )  # fmt: skip


@dataclass(frozen=True)
class LinkedFile:
    """What `link_replaceable` made: a second name for the source's inode, read back."""

    sha256: str  # hex
    identity: FileIdentity
    nlink: int


def device_of_root(root: ConfinedRoot) -> int:
    """`st_dev` of the folder that holds the root's staging: the mount a link would land on."""
    return root.stat(STAGING_DIR).st_dev


# What a file system or a mount says when it will not make a link: the caller copies instead.
# `ENOSPC` and `EDQUOT` are not here: a copy would not fit either, so they are raised as they are.
_NO_LINK = {
    errno.EXDEV: "cross_mount",
    errno.EPERM: "unsupported",  # `fs.protected_hardlinks`, or a file system without links
    errno.EMLINK: "unsupported",
    errno.EOPNOTSUPP: "unsupported",
    errno.ENOTSUP: "unsupported",
}


def link_replaceable(
    source: ConfinedRoot,
    src: str,
    target: ConfinedRoot,
    dst: str,
    *,
    expected_sha256: str,
    parents: bool = False,
    replace: bool = False,
) -> LinkedFile:
    """THE hard link of the library (EF-EXP-015, D9): a second name `dst` for the file `src`, for
    content that can be had again, on one file system and one mount. It is the only road on which
    a file gets a second name; the caller (`KeepCopier`, given the option by the catch-up job) has
    already refused an irreplaceable Unit and a safeguard. What is checked here:

    - `src` is a regular file, never a symbolic link (nothing is ever linked by name or followed);
    - its `nlink` is 1: a name that someone else made (the user, a seedbox) is not ours to extend;
    - the two roots are on one device, and the kernel agrees (`EXDEV` on a bind mount);
    - the link is made on the staging name and renamed into place (`replace` as for a copy);
    - read back: the inode has exactly two names and the digest is `expected_sha256`.

    `LinkUnavailableError` says nothing was placed and a copy is the way; `ENOSPC` and `EDQUOT`
    are raised as they are."""
    if source.lstat_kind(src) == "symlink":
        raise PathEscapeError(f"{src!r} is a symbolic link: never linked")
    if source.lstat_kind(src) != "file":
        raise LinkUnavailableError("not_regular")
    parts = split_rel(dst)
    if not parts:
        raise FileExistsError("the root itself is not a file")
    fd = source.open_file(src, os.O_RDONLY)
    staged = ""
    placed = False
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise LinkUnavailableError("not_regular")
        if before.st_nlink != 1:
            raise LinkUnavailableError("shared_name")
        target.mkdir(STAGING_DIR, parents=True, exist_ok=True, durable=False)
        if device_of_root(source) != device_of_root(target) or (
            before.st_dev != target.stat(STAGING_DIR).st_dev
        ):
            raise LinkUnavailableError("cross_mount")
        if parents and len(parts) > 1:
            target.mkdir("/".join(parts[:-1]), parents=True, exist_ok=True)
        staged = f"{STAGING_DIR}/{uuid.uuid4().hex}"
        try:
            source.link_to(target, src, staged)
        except OSError as error:
            if error.errno in _NO_LINK:
                raise LinkUnavailableError(_NO_LINK[error.errno]) from error
            raise
        after = os.fstat(fd)
        if after.st_nlink != 2 or after.st_ino != before.st_ino:
            raise LinkUnavailableError("unverified")
        if not verify_copy(fd, expected_sha256):
            raise CopyVerificationError("link verification failed: nothing was put in place")
        put_file(target, staged, dst, replace=replace)
        placed = True
        target.fsync_dir("/".join(parts[:-1]) or ".")
        return LinkedFile(expected_sha256, identity_of(after), after.st_nlink)
    finally:
        os.close(fd)
        if staged and not placed:
            _discard(target, staged)


def _clone_into(fd: int, source_fd: int) -> bool:
    """Share the extents of `source_fd` with the empty staged file; False when it cannot."""
    try:
        fcntl.ioctl(fd, FICLONE, source_fd)
    except OSError as error:
        if error.errno in NOT_SUPPORTED:
            return False
        raise
    return True


def _write(
    root: ConfinedRoot,
    rel: str,
    data: bytes | Readable,
    clone_from: int | None,
    *,
    parents: bool,
    replace: bool,
    mtime_ns: int | None,
    verify_digest: Callable[[], str] | None,
    file_mode: int | None,
) -> tuple[FileIdentity, bool]:
    parts = split_rel(rel)
    if not parts:
        raise FileExistsError("the root itself is not a file")
    staging_dir = STAGING_DIR
    root.mkdir(staging_dir, parents=True, exist_ok=True, durable=False)
    staged = f"{staging_dir}/{uuid.uuid4().hex}"
    if file_mode is None and parts[0] == HOST_AREA:
        file_mode = 0o600  # the lease, the journals: owner only, whatever the library's umask
    requested = 0o666 if file_mode is None else file_mode
    fd = root.open_file(staged, os.O_RDWR | os.O_CREAT | os.O_EXCL, requested)
    placed = False
    try:
        if root.umask is not None or file_mode is not None:
            os.fchmod(fd, root.file_mode(requested))
        cloned = clone_from is not None and _clone_into(fd, clone_from)
        if not cloned:
            with os.fdopen(os.dup(fd), "wb", closefd=True) as sink:
                if isinstance(data, bytes):
                    sink.write(data)
                else:
                    shutil.copyfileobj(data, sink, CHUNK)
                sink.flush()
        if mtime_ns is not None:
            os.utime(fd, ns=(mtime_ns, mtime_ns))
        os.fsync(fd)
        if verify_digest is not None and not verify_copy(fd, verify_digest()):
            raise CopyVerificationError("copy verification failed: nothing was put in place")
        identity = identity_of(os.fstat(fd))
        if parents and len(parts) > 1:
            root.mkdir("/".join(parts[:-1]), parents=True, exist_ok=True)
        put_file(root, staged, rel, replace=replace)
        placed = True
        root.fsync_dir("/".join(parts[:-1]) or ".")
        return identity, cloned
    finally:
        os.close(fd)
        if not placed or not replace:
            _discard(root, staged)


def put_file(root: ConfinedRoot, staged: str, rel: str, *, replace: bool) -> None:
    """Rename into place, replacing or not (`FileExistsError`): a rename in both cases, so no
    second name for the staged inode ever exists (never a hard link, EF-BIB-027)."""
    root.rename(staged, rel, replace=replace, durable=False)  # the caller flushes the folder


def _discard(root: ConfinedRoot, staged: str) -> None:
    """A failed write's staged file: the `staging_orphan_cleanup` emitter, inside its perimeter."""
    require_inside("staging_orphan_cleanup", staged)
    with contextlib.suppress(FileNotFoundError):
        root.unlink(staged)
