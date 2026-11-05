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
NINEP_MAGIC = 0x01021997
CEPH_MAGIC = 0x00C36400
AFS_MAGIC = 0x5346414F
FUSE_MAGIC = 0x65735546  # FUSE says nothing of what it serves: /proc/self/mountinfo does
REFUSED_KINDS = {
    "nfs": NFS_MAGIC, "cifs": CIFS_MAGIC, "smb2": SMB2_MAGIC, "smb": SMB_MAGIC,
    "9p": NINEP_MAGIC, "ceph": CEPH_MAGIC, "afs": AFS_MAGIC,
    "fuse.sshfs": FUSE_MAGIC,
}  # fmt: skip
_BY_MAGIC = {magic: kind for kind, magic in REFUSED_KINDS.items() if magic != FUSE_MAGIC}
MOUNTINFO = Path("/proc/self/mountinfo")


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


def fuse_type(path: Path, mountinfo: Path = MOUNTINFO) -> str | None:
    """`fuse.sshfs`, `fuse.rclone`...: the type of the deepest mount that holds `path`."""
    try:
        target = os.path.realpath(path)
        best: tuple[int, str] | None = None
        for line in mountinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            head, _, tail = line.partition(" - ")
            fields = head.split()
            if len(fields) < 5 or not tail:
                continue
            point = fields[4].replace("\\040", " ")
            inside = target == point or target.startswith(point.rstrip("/") + "/")
            if inside and (best is None or len(point) > best[0]):
                best = (len(point), tail.split()[0])
        return best[1] if best else None
    except OSError:
        return None


def filesystem_kind(
    path: Path,
    *,
    statfs: Callable[[Path], int] | None = None,
    mountinfo: Path = MOUNTINFO,
) -> str:
    magic = (statfs or filesystem_magic)(path)
    if magic == FUSE_MAGIC:
        return fuse_type(path, mountinfo) or "fuse"
    return _BY_MAGIC.get(magic, f"0x{magic:x}")


def refuse_network_data_dir(
    path: Path, *, allow: bool, statfs: Callable[[Path], int] | None = None
) -> str | None:
    """The kind of network file system, when `allow` lets it through; None when local."""
    kind = filesystem_kind(path, statfs=statfs)
    if kind == "0x0":  # statfs failed: say so, never refuse on a guess
        structlog.get_logger().warning("data_dir_filesystem_unknown", path=str(path))
        return None
    if kind not in REFUSED_KINDS:
        return None
    if not allow:
        raise NetworkDataDirError(
            f"{path} is on {kind}: the database and the instance lock need a local file system "
            "(set SHELFHOLD_ALLOW_NETWORK_DATA_DIR=true to go on at your own risk)"
        )
    structlog.get_logger().warning("data_dir_on_network_filesystem", path=str(path), kind=kind)
    return kind
