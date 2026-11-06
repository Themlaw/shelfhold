"""What each physical root can do, probed once at start (EF-EXP-059, GAP-7-14).

Three questions, each answered by trying and cleaning up, never by guessing from the file system's
name: can a file be cloned in place (reflink), can a file of the root be hard-linked to `/store`
(the "kept copy" of M1b: when it cannot, the copy is a real copy, EF-EXP-015), and does an inode
survive a rename (a library on a file system that renumbers its inodes breaks the identity of
files). The probe files are drawn by `new_probe_name`, are made at the top of the root, and are
removed before the function returns, whatever happened; nothing is left on a root.
"""

from __future__ import annotations

import errno
import fcntl
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Self

from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.fsops.emitters import new_probe_name

__all__ = ["Link", "RootCapabilities", "probe_root"]

FICLONE: Final = 0x40049409  # _IOW(0x94, 9, int): clone the whole file behind the descriptor
NOT_SUPPORTED: Final = frozenset({errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOTTY})
LINK_REFUSED: Final = frozenset({errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EOPNOTSUPP})
PAYLOAD: Final = b"shelfhold capability probe\n" * 64


class Link(StrEnum):
    YES = "yes"
    NO = "no"
    NOT_APPLICABLE = "not_applicable"  # no /store to link to (M1a: pass-through mode)


@dataclass(frozen=True)
class RootCapabilities:
    root: str
    reflink: bool | None  # None: the probe could not run (the answer is unknown)
    hardlink_store: Link
    inodes_stable: bool | None
    filesystem: str = ""
    probed_at: str = ""
    store_reason: str = ""  # why `hardlink_store` is not applicable

    @property
    def copy_fallback(self) -> bool:
        """With neither a reflink nor a link to `/store`, a kept copy is a real copy."""
        return not self.reflink and self.hardlink_store is not Link.YES

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "reflink": self.reflink,
            "hardlink_store": self.hardlink_store.value,
            "inodes_stable": self.inodes_stable,
            "filesystem": self.filesystem,
            "probed_at": self.probed_at,
            "store_reason": self.store_reason,
            "copy_fallback": self.copy_fallback,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            root=str(data["root"]),
            reflink=data.get("reflink"),
            hardlink_store=Link(data.get("hardlink_store", Link.NOT_APPLICABLE.value)),
            inodes_stable=data.get("inodes_stable"),
            filesystem=str(data.get("filesystem", "")),
            probed_at=str(data.get("probed_at", "")),
            store_reason=str(data.get("store_reason", "")),
        )


def _make(root: ConfinedRoot, names: list[str]) -> str:
    name = new_probe_name()
    fd = root.open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    names.append(name)  # from here on the file exists and is removed
    try:
        os.write(fd, PAYLOAD)
        os.fsync(fd)
    finally:
        os.close(fd)
    return name


def _reflink(root: ConfinedRoot, names: list[str]) -> bool | None:
    source = _make(root, names)
    name = new_probe_name()
    names.append(name)
    destination = root.open_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    source_fd = root.open_file(source, os.O_RDONLY)
    try:
        fcntl.ioctl(destination, FICLONE, source_fd)
    except OSError as error:
        return False if error.errno in NOT_SUPPORTED else None
    finally:
        os.close(source_fd)
        os.close(destination)
    return True


def _inodes(root: ConfinedRoot, names: list[str]) -> bool:
    """Does the inode stay the same across a rename and between `stat` and `fstat`?"""
    first = _make(root, names)
    before = root.stat(first)
    fd = root.open_file(first, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
    finally:
        os.close(fd)
    second = new_probe_name()
    root.rename(first, second, replace=False, durable=False)
    names.remove(first)
    names.append(second)
    after = root.stat(second)
    return before.st_ino != 0 and before.st_ino == opened.st_ino == after.st_ino


def _remove(root: ConfinedRoot, names: list[str]) -> None:
    for name in names:
        try:
            root.unlink(name)
        except FileNotFoundError:
            pass
        except OSError:
            continue  # never hides the answer; a name drawn here is the only thing it could leave
    names.clear()


def probe_root(
    root: ConfinedRoot,
    *,
    store: ConfinedRoot | None,
    filesystem: str,
    now: str,
    store_reason: str = "",
) -> RootCapabilities:
    """Probe a writable root (the caller holds its lease). `store`: the writable `/store` root, or
    None when there is none (M1a), in which case the link probe is not applicable."""
    names: list[str] = []
    answers: dict[str, Any] = {"reflink": None, "inodes": None, "link": Link.NOT_APPLICABLE}
    store_names: list[str] = []
    try:
        try:
            answers["reflink"] = _reflink(root, names)
        except OSError:
            answers["reflink"] = None  # unknown, never a guess
        try:
            answers["inodes"] = _inodes(root, names)
        except OSError:
            answers["inodes"] = None
        try:
            answers["link"] = _hardlink_tracked(root, store, names, store_names)
        except OSError:
            answers["link"] = Link.NOT_APPLICABLE if store is None else Link.NO
    finally:
        _remove(root, names)
        if store is not None:
            _remove(store, store_names)
    return RootCapabilities(
        root=root.path, reflink=answers["reflink"], hardlink_store=answers["link"],
        inodes_stable=answers["inodes"], filesystem=filesystem, probed_at=now,
        store_reason=store_reason if answers["link"] is Link.NOT_APPLICABLE else "",
    )  # fmt: skip


def _hardlink_tracked(
    root: ConfinedRoot, store: ConfinedRoot | None, names: list[str], store_names: list[str]
) -> Link:
    if store is None:
        return Link.NOT_APPLICABLE
    source = _make(root, names)
    name = new_probe_name()
    store_names.append(name)  # removed by the caller whether or not the link was made
    try:
        root.link_to(store, source, name)
    except OSError as error:
        if error.errno in LINK_REFUSED:
            return Link.NO
        raise
    return Link.YES
