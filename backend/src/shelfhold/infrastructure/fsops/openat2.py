"""`openat2(2)` through ctypes (Python's `os` does not expose it).

`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` makes the kernel resolve a path in
one step, refusing any symbolic link, magic link or climb out of the directory descriptor, so there
is no gap between a check and the use (ENF-SEC-012).
"""

import ctypes
import errno
import os
import platform

SYS_OPENAT2 = 437  # the same number on every architecture that has the call
RESOLVE_NO_XDEV = 0x01  # never cross a mount point: an undeclared sub-mount stays out of reach
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
CONFINED = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


_libc = ctypes.CDLL(None, use_errno=True)
_syscall = _libc.syscall
_syscall.restype = ctypes.c_long
_syscall.argtypes = (ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t)


def openat2(dir_fd: int, path: str, flags: int, mode: int = 0, resolve: int = CONFINED) -> int:
    """Open `path` beneath `dir_fd`; raises `OSError` with the kernel's errno."""
    how = _OpenHow(flags | os.O_CLOEXEC, mode, resolve)
    fd = _syscall(SYS_OPENAT2, dir_fd, os.fsencode(path), ctypes.byref(how), ctypes.sizeof(how))
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    return int(fd)


def openat2_supported(root_fd: int | None = None) -> bool:
    """False on kernels older than 5.6 and under seccomp profiles that filter the call.

    The probe opens the root's own descriptor when given one, never the working directory.
    """
    own = root_fd is None
    base: int = os.open("/", os.O_RDONLY | os.O_DIRECTORY) if root_fd is None else root_fd
    try:
        os.close(openat2(base, ".", os.O_RDONLY | os.O_DIRECTORY))
    except OSError as error:
        if error.errno in (errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.E2BIG):
            return False
        raise
    finally:
        if own:
            os.close(base)
    return True


# renameat2(2) is not in Python's `os` either. The number is per architecture.
RENAME_NOREPLACE = 1
RENAME_EXCHANGE = 2
_RENAMEAT2 = {"x86_64": 316, "aarch64": 276, "arm64": 276}.get(platform.machine())
_libc2 = ctypes.CDLL(None, use_errno=True)
_syscall2 = _libc2.syscall
_syscall2.restype = ctypes.c_long
_syscall2.argtypes = (
    ctypes.c_long,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_int,
    ctypes.c_char_p,
    ctypes.c_uint,
)


def renameat2_noreplace(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
    """Rename that fails with `FileExistsError` instead of replacing; `OSError(ENOSYS/EINVAL)`
    when the kernel or the file system cannot do it (the caller falls back)."""
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 number unknown on this architecture")
    result = _syscall2(
        _RENAMEAT2, src_dir_fd, os.fsencode(src), dst_dir_fd, os.fsencode(dst), RENAME_NOREPLACE
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), src)


def renameat2_exchange(a_dir_fd: int, a: str, b_dir_fd: int, b: str) -> None:
    """Swap two names atomically (`RENAME_EXCHANGE`): at every instant each name holds one of the
    two files, never none. `OSError(ENOSYS/EINVAL)` when the kernel or the file system cannot."""
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 number unknown on this architecture")
    result = _syscall2(
        _RENAMEAT2, a_dir_fd, os.fsencode(a), b_dir_fd, os.fsencode(b), RENAME_EXCHANGE
    )
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), a)
