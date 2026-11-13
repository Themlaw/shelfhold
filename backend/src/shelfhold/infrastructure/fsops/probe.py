"""The write probe of a shared folder (EF-UX-002 CLI/API, EF-EXP-054): can the container's uid
write, rename and make a folder there? When not, the cause and the exact compose block to fix it.

Only the write probe: the check that Kavita reads the probe file is a documented manual step
(coupable in M1a). The probe leaves nothing behind: the file it made goes through the closed
`probe_file_cleanup` emitter, the folder it made through `rmdir` (both are names it drew itself).
"""

import contextlib
import errno
import io
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from shelfhold.infrastructure.archives import ArchiveWriter
from shelfhold.infrastructure.fsops.emitters import PROBE_NAME, new_probe_name

STEPS = ("write", "rename", "mkdir")


class ProbeDisk(Protocol):
    def write_atomic(self, local: str, data: bytes, **options: object) -> object: ...
    def rename(self, src: str, dst: str, *, replace: bool = False) -> None: ...
    def mkdir(self, local: str, *, parents: bool = True, exist_ok: bool = True) -> None: ...
    def rmdir(self, local: str) -> None: ...
    def system_unlink(self, emitter: str, local: str) -> None: ...
    def list_dir(self, local: str) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class ProbeReport:
    ok: bool
    folder: str
    steps: tuple[str, ...] = ()
    failed_step: str | None = None
    cause: str | None = None
    event: str | None = None  # the matrix event that announces it
    yaml_hint: str = field(default="", repr=False)
    errno_name: str | None = None  # `EACCES`, `ENOSPC`... or the class of an unexpected error
    error: OSError | None = field(default=None, repr=False)  # the error that stopped the probe


def _errno_name(error: OSError) -> str:
    if error.errno is not None and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return type(error).__name__


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


def probe_failure(folder: str, error: OSError, *, step: str) -> ProbeReport:
    """The report of a probe that could not even start (the lease of the folder's root)."""
    cause, event = _cause_of(error)
    return ProbeReport(
        False, folder, (), step, cause, event,
        _yaml_hint(folder) if event == "target.permission_denied" else "",
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
            _errno_name(error), error,
        )  # fmt: skip
    finally:
        if made_dir:
            _remove_probe_dir(disk, directory)
        if made_file is not None:
            _remove_probe_file(disk, made_file)
    return ProbeReport(True, folder, tuple(done))


def make_folder(disk: ProbeDisk, folder: str) -> None:
    """The folder of a destination, made by the sole writer (the probe and Kavita need it)."""
    disk.mkdir(folder, parents=True, exist_ok=True)


def probe_epub() -> bytes:
    """A small, valid EPUB: the file Kavita is asked to read from the shared folder. Written by
    the one archive writer (`mimetype` first and stored, as the format wants)."""
    out = io.BytesIO()
    with ArchiveWriter(out) as book:
        book.add("mimetype", b"application/epub+zip")
        book.add(
            "META-INF/container.xml",
            b'<?xml version="1.0"?><container version="1.0" '
            b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            b'<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            b"</rootfiles></container>",
            compress=True,
        )
        book.add(
            "content.opf",
            b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            b'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            b'<dc:identifier id="id">shelfhold-probe</dc:identifier><dc:title>probe</dc:title>'
            b"<dc:language>en</dc:language></metadata><manifest>"
            b'<item id="p" href="p.xhtml" media-type="application/xhtml+xml"/></manifest>'
            b'<spine><itemref idref="p"/></spine></package>',
            compress=True,
        )
        book.add(
            "p.xhtml",
            b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>p'
            b"</title></head><body><p>probe</p></body></html>",
            compress=True,
        )
    return out.getvalue()


@contextlib.contextmanager
def readable_probe(disk: ProbeDisk, folder: str) -> Iterator[str]:
    """The file Kavita is asked to read: written by fsops into `folder` (never over a file that
    is there), removed when the block ends, whatever the block did (EF-UX-002)."""
    path = f"{folder.rstrip('/')}/{new_probe_name(epub=True)}"
    disk.write_atomic(path, probe_epub(), replace=False)
    try:
        yield path
    finally:
        _remove_probe_file(disk, path)


def sweep_probe_files(disk: ProbeDisk, folder: str) -> int:
    """Remove the probe files an earlier crash left in `folder` (a regular file whose name is
    exactly what `new_probe_name` draws: 16 hex digits, nothing else is ever touched)."""
    try:
        entries = disk.list_dir(folder)
    except OSError:
        return 0
    swept = 0
    for name, kind in sorted(entries.items()):
        if kind == "file" and PROBE_NAME.fullmatch(name):
            with contextlib.suppress(OSError):
                _remove_probe_file(disk, f"{folder.rstrip('/')}/{name}")
                swept += 1
    return swept
