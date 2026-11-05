"""The provenance mirror's own corner of a root: `<root>/.shelfhold/manifest/` (EF-EXP-063).

Reserved to the host (I37): ordinary paths can never reach it, and a mirror file is named by two
ULIDs, so a name cannot walk out of the folder. Written atomically, `0600` in `0700` folders (the
file system may refuse the modes on some mounts: they are asked, not required). The functions work
on an already opened, already gated root; `Fsops` is the only caller.
"""

import os

from shelfhold.export.domain.provenance_mirror import MAX_MIRROR_BYTES, MIRROR_DIR, mirror_name
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.fsops.errors import ReservedPathError
from shelfhold.infrastructure.fsops.identity import FileIdentity
from shelfhold.infrastructure.fsops.writer import write_atomic

__all__ = ["list_mirror_files", "mirror_rel", "read_mirror_file", "write_mirror_file"]

_MODE_FILE = 0o600
_MODE_DIR = 0o700


def mirror_rel(name: str) -> str:
    """`<dest>/<series>.jsonl` -> the path under the root, or `ReservedPathError`."""
    parts = name.split("/")
    if len(parts) != 2:
        raise ReservedPathError(f"{name!r} is not a mirror file name")
    try:
        mirror_name(parts[0], parts[1].removesuffix(".jsonl"))
    except ValueError:
        raise ReservedPathError(f"{name!r} is not a mirror file name") from None
    return f"{MIRROR_DIR}/{name}"


def write_mirror_file(root: ConfinedRoot, name: str, data: bytes) -> FileIdentity:
    rel = mirror_rel(name)
    folder = rel.rsplit("/", 1)[0]
    root.mkdir(folder, _MODE_DIR, parents=True, exist_ok=True)
    return write_atomic(root, rel, data, parents=True, replace=True, file_mode=_MODE_FILE)


def read_mirror_file(root: ConfinedRoot, name: str) -> bytes | None:
    """The file, or None when there is none; never more than the parser accepts plus one byte."""
    try:
        fd = root.open_file(mirror_rel(name), os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        return stream.read(MAX_MIRROR_BYTES + 1)


def list_mirror_files(root: ConfinedRoot) -> list[str]:
    """Every `<dest>/<series>.jsonl` under the folder, sorted; anything else there is ignored."""
    found: list[str] = []
    try:
        folders = root.listdir(MIRROR_DIR)
    except FileNotFoundError:
        return found
    for folder in sorted(folders):
        try:
            names = root.listdir(f"{MIRROR_DIR}/{folder}")
        except OSError:  # a file, a link, a folder we cannot open: not ours
            continue
        for file in sorted(names):
            try:
                mirror_rel(f"{folder}/{file}")
            except ReservedPathError:
                continue
            found.append(f"{folder}/{file}")
    return found
