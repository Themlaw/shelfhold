"""A directory descriptor and the operations beneath it (ENF-SEC-012).

Every path is relative to the root descriptor and is resolved in the same call that uses it:
`openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` where the kernel has
it, otherwise one `O_NOFOLLOW` open per component with `dir_fd`. There is no `realpath`, no
`resolve`, no check followed by a separate use. The path of a root itself comes from the
administrator's configuration and may be reached through a link (a bind mount); nothing below it
may.
"""

import errno
import os
import stat
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

from shelfhold.infrastructure.fsops.errors import FsopsError, NotRegularFileError, PathEscapeError
from shelfhold.infrastructure.fsops.openat2 import (
    CONFINED,
    openat2,
    openat2_supported,
    renameat2_noreplace,
)

__all__ = ["ConfinedRoot", "Strategy"]

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# What the kernel says when a link or a climb stops a resolution.
ESCAPE_ERRNOS = (errno.ELOOP, errno.EXDEV)
Kind = Literal["file", "dir", "symlink", "other"]


def mount_id_of(fd: int) -> int | None:
    """The mount a descriptor lives on, from `/proc/self/fdinfo`; None when unreadable."""
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
            for line in info:
                if line.startswith("mnt_id:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


HOST_AREA = ".shelfhold"  # == physical_roots.RESERVED_DIR (this layer cannot import it)


class Strategy(StrEnum):
    AUTO = "auto"
    OPENAT2 = "openat2"
    FALLBACK = "fallback"


def split_rel(rel: str) -> list[str]:
    """The components of a relative path; absolute paths, `..` and NUL are refused."""
    if not isinstance(rel, str) or "\0" in rel:
        raise PathEscapeError("path is not a text without NUL")
    if rel.startswith("/"):
        raise PathEscapeError(f"absolute path refused: {rel!r}")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PathEscapeError(f"'..' refused: {rel!r}")
    if not parts and rel == "":
        raise PathEscapeError("empty path")
    return parts


class ConfinedRoot:
    def __init__(self, fd: int, path: str, strategy: Strategy, umask: int | None) -> None:
        self._fd = fd
        self.path = path
        self.strategy = strategy
        self.umask = umask
        self.st_dev = os.fstat(fd).st_dev
        self._mount = mount_id_of(fd)

    @classmethod
    def open(
        cls, path: str | Path, *, strategy: Strategy = Strategy.AUTO, umask: int | None = None
    ) -> Self:
        fd = os.open(os.fspath(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            if strategy is Strategy.AUTO:
                strategy = Strategy.OPENAT2 if openat2_supported(fd) else Strategy.FALLBACK
            elif strategy is Strategy.OPENAT2 and not openat2_supported(fd):
                raise FsopsError("openat2 is not available on this kernel")
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, os.fspath(path), strategy, umask)

    # -- lifecycle --------------------------------------------------------------------------

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _root_fd(self) -> int:
        if self._fd < 0:
            raise FsopsError("root is closed")
        return self._fd

    # -- permissions ------------------------------------------------------------------------

    def file_mode(self, requested: int = 0o666) -> int:
        """The mode to create with; with an explicit umask it is applied here, never globally."""
        return requested if self.umask is None else requested & ~self.umask

    # -- resolution -------------------------------------------------------------------------

    def _open_dir_at(self, dir_fd: int, name: str) -> int:
        try:
            fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
        except OSError as error:
            raise self._translate(error, dir_fd, name) from error
        if not self._on_root_mount(fd):
            os.close(fd)
            raise PathEscapeError(f"{name!r} is on another mount than the root")
        return fd

    def _on_root_mount(self, fd: int) -> bool:
        """Same mount as the root. `mnt_id` tells two mounts of one device apart; where
        `/proc/self/fdinfo` is unreadable only `st_dev` is compared, so a bind mount of the same
        device is then not detected (openat2's RESOLVE_NO_XDEV has no such limit)."""
        if self._mount is not None:
            mount = mount_id_of(fd)
            if mount is not None:
                return mount == self._mount
        return os.fstat(fd).st_dev == self.st_dev

    @staticmethod
    def _translate(error: OSError, dir_fd: int | None, name: str) -> OSError:
        """A link or a climb is an escape; anything else keeps its own errno."""
        if error.errno in ESCAPE_ERRNOS:
            return PathEscapeError(f"{name!r} goes through a symbolic link or out of the root")
        if error.errno in (errno.ENOTDIR, errno.EMLINK) and dir_fd is not None:
            try:
                if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                    return PathEscapeError(f"{name!r} is a symbolic link")
            except OSError:
                pass
        return error

    def _walk(self, parts: list[str]) -> int:
        """A descriptor on the directory `parts` beneath the root; the caller closes it."""
        root = self._root_fd()
        if not parts:
            return os.dup(root)
        if self.strategy is Strategy.OPENAT2:
            try:
                return openat2(root, "/".join(parts), os.O_RDONLY | os.O_DIRECTORY, 0, CONFINED)
            except OSError as error:
                raise self._translate(error, None, "/".join(parts)) from error
        current = os.dup(root)
        try:
            for part in parts:
                following = self._open_dir_at(current, part)
                os.close(current)
                current = following
        except BaseException:
            os.close(current)
            raise
        return current

    def _parent(self, rel: str) -> tuple[int, str]:
        parts = split_rel(rel)
        if not parts:
            raise PathEscapeError("the root itself cannot be the subject of this operation")
        return self._walk(parts[:-1]), parts[-1]

    # -- operations -------------------------------------------------------------------------

    def open_file(self, rel: str, flags: int, mode: int = 0o666) -> int:
        """Open a file beneath the root; the last component is never followed if a link."""
        parts = split_rel(rel)
        if not parts:
            raise PathEscapeError("the root itself is not a file")
        # O_NONBLOCK: opening a FIFO must not wait for a writer; only regular files are returned.
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        # openat2 insists on a zero mode when nothing is created.
        create_mode = self.file_mode(mode) if flags & os.O_CREAT else 0
        if self.strategy is Strategy.OPENAT2:
            try:
                fd = openat2(self._root_fd(), "/".join(parts), flags, create_mode, CONFINED)
            except OSError as error:
                raise self._translate(error, None, rel) from error
        else:
            parent = self._walk(parts[:-1])
            try:
                fd = os.open(parts[-1], flags, create_mode, dir_fd=parent)
            except OSError as error:
                raise self._translate(error, parent, parts[-1]) from error
            finally:
                os.close(parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NotRegularFileError(f"{rel!r} is not a regular file")
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def open_dir(self, rel: str = ".") -> int:
        return self._walk(split_rel(rel))

    def stat(self, rel: str) -> os.stat_result:
        """`lstat` semantics: a link is described, never followed."""
        if not split_rel(rel):
            return os.fstat(self._root_fd())
        parent, name = self._parent(rel)
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)

    def lstat_kind(self, rel: str) -> Kind | None:
        try:
            mode = self.stat(rel).st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISDIR(mode):
            return "dir"
        return "file" if stat.S_ISREG(mode) else "other"

    def listdir(self, rel: str = ".") -> list[str]:
        fd = self._walk(split_rel(rel))
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)

    def mkdir(
        self,
        rel: str,
        mode: int = 0o777,
        *,
        parents: bool = False,
        exist_ok: bool = False,
        durable: bool = True,
    ) -> None:
        parts = split_rel(rel)
        if not parts:
            if exist_ok:
                return
            raise FileExistsError(errno.EEXIST, "the root exists", rel)
        if parts[0] == HOST_AREA:
            mode = 0o700  # Shelfhold's own area: owner only, whatever the library's umask
        dir_mode = self.file_mode(mode)
        if parents:
            current = os.dup(self._root_fd())
            try:
                for index, part in enumerate(parts):
                    last = index == len(parts) - 1
                    created = self._mkdir_at(current, part, dir_mode, exist_ok or not last)
                    following = self._open_dir_at(current, part)
                    try:
                        if created:
                            self._set_mode(following, mode)
                            if durable:
                                os.fsync(current)
                    except BaseException:
                        os.close(following)
                        raise
                    os.close(current)
                    current = following
            finally:
                os.close(current)
            return
        parent, name = self._parent(rel)
        try:
            created = self._mkdir_at(parent, name, dir_mode, exist_ok)
            if created and self.umask is not None:
                child = self._open_dir_at(parent, name)
                try:
                    self._set_mode(child, mode)
                finally:
                    os.close(child)
            if created and durable:
                os.fsync(parent)
        finally:
            os.close(parent)

    def _mkdir_at(self, dir_fd: int, name: str, mode: int, exist_ok: bool) -> bool:
        try:
            os.mkdir(name, mode, dir_fd=dir_fd)
        except FileExistsError:
            kind = stat.S_IFMT(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
            if kind == stat.S_IFLNK:
                raise PathEscapeError(f"{name!r} is a symbolic link") from None
            if not exist_ok or kind != stat.S_IFDIR:
                raise
            return False
        return True

    def _set_mode(self, dir_fd: int, mode: int) -> None:
        if self.umask is not None:
            os.fchmod(dir_fd, self.file_mode(mode))

    def unlink(self, rel: str) -> None:
        parent, name = self._parent(rel)
        try:
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)

    def rmdir(self, rel: str) -> None:
        parent, name = self._parent(rel)
        try:
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(parent)

    def rename(self, src: str, dst: str, *, replace: bool, durable: bool = True) -> None:
        """Rename within this root. `replace=False` fails with `FileExistsError` rather than
        overwrite (renameat2 RENAME_NOREPLACE); `replace=True` is the POSIX replacing rename, for
        the atomic write of a file that is meant to be replaced.

        `durable` flushes both parent folders before returning, so the rename survives a crash
        before the caller commits `done` to the journal (EF-EXP-041); the atomic writer flushes
        the folder itself and turns it off."""
        src_parent, src_name = self._parent(src)
        try:
            dst_parent, dst_name = self._parent(dst)
            try:
                if replace:
                    os.rename(src_name, dst_name, src_dir_fd=src_parent, dst_dir_fd=dst_parent)
                else:
                    self._rename_noreplace(src_parent, src_name, dst_parent, dst_name)
                if durable:
                    os.fsync(src_parent)
                    os.fsync(dst_parent)
            finally:
                os.close(dst_parent)
        finally:
            os.close(src_parent)

    @staticmethod
    def _rename_noreplace(src_parent: int, src: str, dst_parent: int, dst: str) -> None:
        try:
            renameat2_noreplace(src_parent, src, dst_parent, dst)
        except OSError as error:
            if error.errno not in (errno.ENOSYS, errno.EINVAL):
                raise
            # No renameat2 here (old kernel, or a file system without it): a hard link fails if
            # the target exists, which is the same guarantee for a regular file.
            if not stat.S_ISREG(os.stat(src, dir_fd=src_parent, follow_symlinks=False).st_mode):
                raise
            os.link(src, dst, src_dir_fd=src_parent, dst_dir_fd=dst_parent, follow_symlinks=False)
            os.unlink(src, dir_fd=src_parent)

    def link(self, src: str, dst: str) -> None:
        """A hard link that fails with `FileExistsError`: the no-clobber way to put a file."""
        src_parent, src_name = self._parent(src)
        try:
            dst_parent, dst_name = self._parent(dst)
            try:
                os.link(
                    src_name,
                    dst_name,
                    src_dir_fd=src_parent,
                    dst_dir_fd=dst_parent,
                    follow_symlinks=False,
                )
            finally:
                os.close(dst_parent)
        finally:
            os.close(src_parent)

    def utime(self, rel: str, ns: tuple[int, int]) -> None:
        parent, name = self._parent(rel)
        try:
            os.utime(name, ns=ns, dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)

    def fsync_dir(self, rel: str = ".") -> None:
        fd = self._walk(split_rel(rel))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def remove_tree(self, rel: str) -> None:
        """Delete a directory's content and the directory, never following a link."""
        parts = split_rel(rel)
        fd = self._walk(parts)
        try:
            self._empty(fd)
        finally:
            os.close(fd)
        if parts:
            self.rmdir(rel)

    def empty_dir(self, rel: str) -> int:
        """Remove everything inside a directory; returns the number of top-level entries."""
        fd = self._walk(split_rel(rel))
        try:
            return self._empty(fd)
        finally:
            os.close(fd)

    def _empty(self, dir_fd: int) -> int:
        names = os.listdir(dir_fd)
        for name in names:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                child = self._open_dir_at(dir_fd, name)
                try:
                    self._empty(child)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=dir_fd)
            else:
                os.unlink(name, dir_fd=dir_fd)
        return len(names)
