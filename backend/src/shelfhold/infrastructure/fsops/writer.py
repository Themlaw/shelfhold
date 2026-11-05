"""Atomic writes inside one root: staging on the same mount, fsync, rename, fsync of the folder.

The file is built under `<root>/.shelfhold/staging/<uuid>`, which is on the same file system as
its destination (no `EXDEV`), flushed, then renamed to its place relative to descriptors, and the
destination folder is flushed. A reader (Kavita) sees the old file or the new one, never half of
one (EF-EXP-006). No `chown`: the files belong to the container's user, and the umask applies.
"""

import contextlib
import hashlib
import os
import shutil
import uuid
from collections.abc import Callable
from typing import Protocol

from shelfhold.export.domain.physical_roots import RECYCLE_DIR, STAGING_DIR
from shelfhold.infrastructure.fsops.confined import ConfinedRoot, split_rel
from shelfhold.infrastructure.fsops.emitters import require_inside
from shelfhold.infrastructure.fsops.errors import CopyVerificationError
from shelfhold.infrastructure.fsops.identity import FileIdentity, identity_of

CHUNK = 1 << 20

__all__ = [
    "HashingReader",
    "Readable",
    "ensure_host_dirs",
    "put_file",
    "verify_copy",
    "write_atomic",
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
    parts = split_rel(rel)
    if not parts:
        raise FileExistsError("the root itself is not a file")
    staging_dir = STAGING_DIR
    root.mkdir(staging_dir, parents=True, exist_ok=True, durable=False)
    staged = f"{staging_dir}/{uuid.uuid4().hex}"
    requested = 0o666 if file_mode is None else file_mode
    fd = root.open_file(staged, os.O_RDWR | os.O_CREAT | os.O_EXCL, requested)
    placed = False
    try:
        if root.umask is not None or file_mode is not None:
            os.fchmod(fd, root.file_mode(requested))
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
        return identity
    finally:
        os.close(fd)
        if not placed or not replace:
            _discard(root, staged)


def put_file(root: ConfinedRoot, staged: str, rel: str, *, replace: bool) -> None:
    """Rename into place (replacing), or link without clobbering (`FileExistsError`)."""
    if replace:
        root.rename(staged, rel, replace=True, durable=False)  # the caller flushes the folder
    else:
        root.link(staged, rel)


def _discard(root: ConfinedRoot, staged: str) -> None:
    """A failed write's staged file: the `staging_orphan_cleanup` emitter, inside its perimeter."""
    require_inside("staging_orphan_cleanup", staged)
    with contextlib.suppress(FileNotFoundError):
        root.unlink(staged)
