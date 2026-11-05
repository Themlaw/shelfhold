"""`/data` must not live on NFS or CIFS (ENF-OPS-010): SQLite and `flock` need a local file system.

The type comes from `statfs(2)`, read through libc (Python's `os.statvfs` has no `f_type`).
`/store` and `/library` may be network mounts: their sentinel guards them (EF-EXP-029).
"""

import ctypes
import ctypes.util
import os
from collections.abc import Callable
from pathlib import Path

import structlog

NFS_MAGIC = 0x6969
CIFS_MAGIC = 0xFF534D42
SMB2_MAGIC = 0xFE534D42
SMB_MAGIC = 0x517B
REFUSED_KINDS = {"nfs": NFS_MAGIC, "cifs": CIFS_MAGIC, "smb2": SMB2_MAGIC, "smb": SMB_MAGIC}
_BY_MAGIC = {magic: kind for kind, magic in REFUSED_KINDS.items()}


class NetworkDataDirError(RuntimeError):
    """The start is refused: `/data` is on a network file system."""

    code = "data_dir.network_filesystem"


def filesystem_magic(path: Path) -> int:
    """`f_type` of the file system under `path`; 0 when it cannot be read."""
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    buffer = ctypes.create_string_buffer(256)  # larger than `struct statfs` on any Linux ABI
    if libc.statfs(os.fsencode(path), buffer) != 0:
        return 0
    return int(ctypes.c_long.from_buffer(buffer).value) & 0xFFFFFFFF


def filesystem_kind(path: Path, *, statfs: Callable[[Path], int] | None = None) -> str:
    magic = (statfs or filesystem_magic)(path)
    return _BY_MAGIC.get(magic, f"0x{magic:x}")


def refuse_network_data_dir(
    path: Path, *, allow: bool, statfs: Callable[[Path], int] | None = None
) -> str | None:
    """The kind of network file system, when `allow` lets it through; None when local."""
    kind = filesystem_kind(path, statfs=statfs)
    if kind not in REFUSED_KINDS:
        return None
    if not allow:
        raise NetworkDataDirError(
            f"{path} is on {kind}: the database and the instance lock need a local file system "
            "(set SHELFHOLD_ALLOW_NETWORK_DATA_DIR=true to go on at your own risk)"
        )
    structlog.get_logger().warning("data_dir_on_network_filesystem", path=str(path), kind=kind)
    return kind
