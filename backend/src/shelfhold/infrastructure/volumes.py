"""The two volumes of ENF-OPS-012: /data (hot, local) and /store (kept copies)."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import structlog

from shelfhold.config import CONFIG_FILE_NAME, CONFIG_SCHEMA_VERSION, Settings
from shelfhold.infrastructure.observability.network_fs import refuse_network_data_dir
from shelfhold.infrastructure.secrets.atomic import write_private


def data_dir_writable_by_others(data_dir: Path) -> bool:
    """True when the group or the world can write in `/data`: they could then replace the local
    CLI socket (EF-USR-024). The image creates it 0755; a host folder made under `umask 002`
    is 0775, so this is a warning, not a refusal (the CLI also checks who it talks to)."""
    try:
        return bool(os.stat(data_dir).st_mode & 0o022)
    except OSError:
        return False


def ensure_volumes(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    refuse_network_data_dir(settings.data_dir, allow=settings.allow_network_data_dir)
    if data_dir_writable_by_others(settings.data_dir):
        structlog.get_logger().warning("data_dir_writable_by_others", path=str(settings.data_dir))
    settings.store_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "tmp").mkdir(exist_ok=True)  # staging of received files (ENF-SEC-002)
    config = settings.data_dir / CONFIG_FILE_NAME
    if not config.exists():
        temporary = config.with_name(config.name + ".tmp")
        temporary.write_text(f'schema_version = "{CONFIG_SCHEMA_VERSION}"\n', encoding="utf-8")
        os.replace(temporary, config)


def write_data_file(data_dir: Path, rel: str, data: bytes) -> None:
    """Atomically (re)write a regenerable file under /data, e.g. `meta/units/01/<ulid>.json`.

    `/data` is not a confined library root: the content comes from the database, the path from an
    identifier the caller validated. Still, `rel` must be relative with plain names, and no folder
    on the way may be a link (a planted link would send the write elsewhere). Owner-only.
    """
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"not a plain relative path: {rel!r}")
    folder = data_dir
    for part in parts[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise ValueError(f"{folder} is a link")
    folder.mkdir(parents=True, exist_ok=True)
    write_private(data_dir.joinpath(*parts), data, overwrite=True)


def read_data_file(path: Path, limit: int) -> bytes:
    """The file's bytes, never following a link and never more than `limit`.

    `FileNotFoundError` when absent. A link, a special file or an oversized file comes back as
    bytes that cannot parse (the caller then treats the file as corrupt and replaces it)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError:  # ELOOP: a link; others: unreadable
        return b""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return b""
        with os.fdopen(os.dup(fd), "rb") as stream:
            return stream.read(limit)
    finally:
        os.close(fd)


def _plain_parts(rel: str) -> tuple[str, ...]:
    path = PurePosixPath(rel)
    if not path.parts or path.is_absolute() or any(p in ("", ".", "..") for p in path.parts):
        raise ValueError(f"not a plain relative path: {rel!r}")
    return path.parts


def erase_data_path(data_dir: Path, rel: str) -> bool:
    """Remove a file or a folder under /data that `purge --now` names (EF-BIB-052): the unit.json
    projection, the thumbnails and the content cache of a Unit. Returns whether something went.

    `/data` is not a confined library root and the path comes from an identifier the caller
    validated, so: `rel` must be relative with plain names, no folder on the way may be a link,
    and a link at the end is removed as a link, never followed. Only the purge calls it."""
    parts = _plain_parts(rel)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = [os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        for part in parts[:-1]:  # dirfd walk, O_NOFOLLOW: a swap for a link cannot redirect it
            try:
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            except FileNotFoundError:
                return False
            except OSError as error:  # ELOOP / ENOTDIR: a link or a file on the way
                raise ValueError(f"{part!r} on the way to {rel!r} is not a plain folder") from error
        parent, name = fds[-1], parts[-1]
        try:
            mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(mode):
            _remove_tree_at(parent, name)
        else:
            os.unlink(name, dir_fd=parent)  # a link at the end is removed as a link
        return True
    finally:
        for fd in fds:
            os.close(fd)


def _remove_tree_at(parent: int, name: str) -> None:
    """Depth first by descriptor; a link inside is unlinked and never entered."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        for child in os.listdir(fd):
            if stat.S_ISDIR(os.stat(child, dir_fd=fd, follow_symlinks=False).st_mode):
                _remove_tree_at(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)


def _unlink_cache_file(data_dir: Path, rel: str) -> bool:
    """Remove one file of the derived caches under `/data` (EF-BIB-018): the eraser of the
    retention sweep, and nothing else — `cache_quotas.erase_cache_path` checks its closed
    allowlist first and is its only caller. Returns whether something went; a path that is not
    there any more answers False (nothing to erase, not a refusal: the row that names it goes).

    A regular file or a link only, never a folder — not even as a tree, unlike
    `erase_data_path` above: what the sweep takes is one file it made itself. Every step of the
    walk is opened with `O_NOFOLLOW` (a link on the way is refused, not followed) and must sit
    on the same device as `/data` itself: a mount point inside the path — or at its end — is
    refused (`ValueError`), so the eraser never crosses into another volume. An erasure that
    fails (`OSError`) is the caller's to log and skip."""
    parts = rel.split("/")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = [os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        device = os.fstat(fds[0]).st_dev
        for part in parts[:-1]:  # dirfd walk, O_NOFOLLOW: a swap for a link cannot redirect it
            try:
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            except FileNotFoundError:
                return False  # a folder of the way is gone: nothing to erase, not a refusal
            except OSError as error:  # ELOOP / ENOTDIR: a link or a file on the way
                raise ValueError(f"{part!r} on the way to {rel!r} is not a plain folder") from error
            if os.fstat(fds[-1]).st_dev != device:
                raise ValueError(f"{rel!r} crosses a mount point at {part!r}")
        parent, name = fds[-1], parts[-1]
        try:
            found = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if found.st_dev != device:
            raise ValueError(f"{rel!r} sits on another volume than /data")
        if not (stat.S_ISREG(found.st_mode) or stat.S_ISLNK(found.st_mode)):
            raise ValueError(f"{rel!r} is not a file: a folder is never removed as a tree")
        try:
            os.unlink(name, dir_fd=parent)  # a link at the end is removed as a link
        except FileNotFoundError:
            return False  # gone between the look and the take: nothing to erase
        return True
    finally:
        for fd in fds:
            os.close(fd)


class DataFolder:
    """A folder of /data held open by its descriptor: what is read through it is read *there*,
    whatever is swapped for the path afterwards (no check-then-open window)."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def names(self) -> list[str]:
        return os.listdir(self._fd)

    def stat_of(self, name: str) -> os.stat_result | None:
        """What `name` of the folder is, a link taken as a link (never followed); None when it is
        not there or is not a plain name."""
        if not name or "/" in name or name in (".", ".."):
            return None
        try:
            return os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        except OSError:
            return None

    def is_regular(self, name: str) -> bool:
        """`name` is a regular file of the folder, not a link, a folder or a special file (nothing
        is opened or read)."""
        if not name or "/" in name or name in (".", ".."):
            return False
        try:
            return stat.S_ISREG(os.stat(name, dir_fd=self._fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def touch(self, name: str, *, at: float | None = None) -> None:
        """Set the access and modification times of `name` (`at`, or now when None): the last
        access of a cache file, its LRU key (EF-BIB-018). A plain name only, never a link."""
        if not name or "/" in name or name in (".", ".."):
            raise ValueError(f"not a plain name: {name!r}")
        try:  # the file is opened without following a link, and the date goes to that very file
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self._fd
            )
        except FileNotFoundError:
            raise
        except OSError as error:  # ELOOP: a link
            raise ValueError(f"{name!r} is a link or unreadable") from error
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"{name!r} is not a regular file")
            os.utime(fd, (at, at) if at is not None else None)
        finally:
            os.close(fd)

    def read(self, name: str, limit: int) -> bytes:
        """The regular file `name` of the folder, never through a link, at most `limit` bytes.
        `ValueError` for a link, a special file or a name that is not plain."""
        if not name or "/" in name or name in (".", ".."):
            raise ValueError(f"not a plain name: {name!r}")
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=self._fd
            )
        except FileNotFoundError:
            raise
        except OSError as error:  # ELOOP: a link
            raise ValueError(f"{name!r} is a link or unreadable") from error
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError(f"{name!r} is not a regular file")
            with os.fdopen(os.dup(fd), "rb") as stream:
                return stream.read(limit)
        finally:
            os.close(fd)


def touch_data_file(folder: DataFolder, name: str, *, at: float) -> None:
    """Record the access of one cache file of an open folder (`at`): the last access is the LRU
    key of the derived caches (EF-BIB-018). The write of the date goes through the `/data`
    writer — never around it — for the reader that holds the folder open."""
    folder.touch(name, at=at)


@contextmanager
def open_data_folder(data_dir: Path, folder: str) -> Iterator[DataFolder]:
    """Open `<data_dir>/<folder>` without following a link on the way (`ValueError`), or
    `FileNotFoundError`."""
    parts = _plain_parts(folder)
    fds = [os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)]
    try:
        for part in parts:
            try:
                fds.append(
                    os.open(
                        part,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=fds[-1],
                    )
                )
            except FileNotFoundError:
                raise
            except OSError as error:
                raise ValueError(f"{part!r} is not a plain folder") from error
        yield DataFolder(fds[-1])
    finally:
        for fd in reversed(fds):
            os.close(fd)
