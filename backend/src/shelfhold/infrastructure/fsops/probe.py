"""The write probe of a shared folder (EF-UX-002 CLI/API, EF-EXP-054): can the container's uid
write, rename and make a folder there? When not, the cause and the exact compose block to fix it.

Only the write probe: the check that Kavita reads the probe file is a documented manual step
(coupable in M1a). The probe leaves nothing behind: the file it made goes through the closed
`probe_file_cleanup` emitter, the folder it made through `rmdir` (both are names it drew itself).
"""

import errno
import os
from dataclasses import dataclass, field
from typing import Protocol

from shelfhold.infrastructure.fsops.emitters import new_probe_name

STEPS = ("write", "rename", "mkdir")


class ProbeDisk(Protocol):
    def write_atomic(self, local: str, data: bytes, **options: object) -> object: ...
    def rename(self, src: str, dst: str, *, replace: bool = False) -> None: ...
    def mkdir(self, local: str, *, parents: bool = True, exist_ok: bool = True) -> None: ...
    def rmdir(self, local: str) -> None: ...
    def system_unlink(self, emitter: str, local: str) -> None: ...


@dataclass(frozen=True)
class ProbeReport:
    ok: bool
    folder: str
    steps: tuple[str, ...] = ()
    failed_step: str | None = None
    cause: str | None = None
    event: str | None = None  # the matrix event that announces it
    yaml_hint: str = field(default="", repr=False)


def _cause_of(error: OSError) -> tuple[str, str | None]:
    if getattr(error, "code", None) == "disk.low":  # DiskLowError, without importing it
        return "disk_low", "disk.low"
    if error.errno in (errno.EACCES, errno.EPERM):
        return "permission_denied", "target.permission_denied"
    if error.errno == errno.EROFS:
        return "read_only_filesystem", "target.permission_denied"
    return type(error).__name__, None


def _yaml_hint(folder: str) -> str:
    try:
        owner = os.stat(folder)
        user = f'    user: "{owner.st_uid}:{owner.st_gid}"\n'
    except OSError:
        user = '    user: "<uid>:<gid>"  # the owner of the folder on the host\n'
    return (
        "services:\n  shelfhold:\n" + user
        + f"    volumes:\n      - <host path>:{folder}:rw\n"
    )  # fmt: skip


def _remove_probe_file(disk: ProbeDisk, path: str) -> None:
    disk.system_unlink("probe_file_cleanup", path)


def _remove_probe_dir(disk: ProbeDisk, path: str) -> None:
    disk.rmdir(path)


def probe_folder(disk: ProbeDisk, folder: str) -> ProbeReport:
    """Write a probe file, rename it, make a folder, then remove all three."""
    base = folder.rstrip("/")
    first, second, directory = (f"{base}/{new_probe_name()}" for _ in range(3))
    done: list[str] = []
    made_file: str | None = None
    made_dir = False
    step = "write"
    try:
        disk.write_atomic(first, b"shelfhold probe\n", replace=False)
        made_file = first
        done.append("write")
        step = "rename"
        disk.rename(first, second)
        made_file = second
        done.append("rename")
        step = "mkdir"
        disk.mkdir(directory, parents=False, exist_ok=False)  # never adopt a folder we did not make
        made_dir = True
        done.append("mkdir")
    except OSError as error:
        cause, event = _cause_of(error)
        return ProbeReport(
            False, folder, tuple(done), step, cause, event,
            _yaml_hint(folder) if event == "target.permission_denied" else "",
        )  # fmt: skip
    finally:
        if made_dir:
            _remove_probe_dir(disk, directory)
        if made_file is not None:
            _remove_probe_file(disk, made_file)
    return ProbeReport(True, folder, tuple(done))
