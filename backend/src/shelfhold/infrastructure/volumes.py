"""The two volumes of ENF-OPS-012: /data (hot, local) and /store (kept copies)."""

import os
import stat
from pathlib import Path, PurePosixPath

import structlog

from shelfhold.config import CONFIG_FILE_NAME, CONFIG_SCHEMA_VERSION, Settings
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
    folder = data_dir
    for part in parts[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise ValueError(f"{folder} is a link")
    target = folder / parts[-1]
    if target.is_symlink() or target.is_file():
        os.unlink(target)
        return True
    if target.is_dir():
        _remove_tree(target)
        return True
    return False


def _remove_tree(folder: Path) -> None:
    """Depth first, a link inside is unlinked and never entered."""
    with os.scandir(folder) as entries:
        for entry in list(entries):
            if entry.is_dir(follow_symlinks=False):
                _remove_tree(Path(entry.path))
            else:
                os.unlink(entry.path)
    os.rmdir(folder)
