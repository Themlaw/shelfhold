"""`openat2(2)` through ctypes (Python's `os` does not expose it).

`RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS` makes the kernel resolve a path in
one step, refusing any symbolic link, magic link or climb out of the directory descriptor, so there
is no gap between a check and the use (ENF-SEC-012).
"""

import ctypes
import errno
import functools
import os

SYS_OPENAT2 = 437  # the same number on every architecture that has the call
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
CONFINED = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS


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


@functools.cache
def openat2_supported() -> bool:
    """False on kernels older than 5.6 and under seccomp profiles that filter the call."""
    here = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.close(openat2(here, ".", os.O_RDONLY))
    except OSError as error:
        if error.errno in (errno.ENOSYS, errno.EPERM, errno.EINVAL, errno.E2BIG):
            return False
        raise
    finally:
        os.close(here)
    return True
