"""What each physical root can do, probed once at start (EF-EXP-059, GAP-7-14).

Three questions, each answered by trying and cleaning up, never by guessing from the file system's
name: can a file be cloned in place (reflink), can a file of the root be hard-linked to `/store`
(the "kept copy" of M1b: when it cannot, the copy is a real copy, EF-EXP-015), and does an inode
survive a rename (a library on a file system that renumbers its inodes breaks the identity of
files). The probe files are drawn by `new_probe_name`, are made in the root's staging folder
and are removed before the function returns, whatever happened; nothing is left on a root.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Self

from shelfhold.export.domain.physical_roots import STAGING_DIR
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.fsops.emitters import new_probe_name

__all__ = ["Link", "RootCapabilities", "probe_root"]

FICLONE: Final = 0x40049409  # _IOW(0x94, 9, int): clone the whole file behind the descriptor
NOT_SUPPORTED: Final = frozenset({errno.EOPNOTSUPP, errno.EXDEV, errno.EINVAL, errno.ENOTTY})
LINK_REFUSED: Final = frozenset({errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EOPNOTSUPP})
PROBE_DIR: Final = STAGING_DIR  # reserved to the host, on the disk of the root
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
    unprobed: str = ""  # why the root was not probed at this start (it is not ready to be written)

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
            "unprobed": self.unprobed,
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
            unprobed=str(data.get("unprobed", "")),
        )


Remove = Callable[[str, str], None]  # (which root, rel path): the closed emitter's removal


@dataclass
class _Made:
    """The names this probe created, per root, in the order made. A name is written down only once
    the file exists because of us (`O_EXCL` or a link that succeeded): a collision with a file that
    was already there is an error, and that file is never removed."""

    remove: Remove
    mine: list[tuple[str, str]] = field(default_factory=list)

    def create(self, which: str, root: ConfinedRoot) -> str:
        rel = f"{PROBE_DIR}/{new_probe_name()}"
        fd = root.open_file(rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.mine.append((which, rel))
        try:
            os.write(fd, PAYLOAD)
            os.fsync(fd)
        finally:
            os.close(fd)
        return rel

    def clean(self) -> None:
        for which, rel in reversed(self.mine):
            with contextlib.suppress(OSError):  # never hides the answer
                self.remove(which, rel)
        self.mine.clear()


def _reflink(root: ConfinedRoot, made: _Made) -> bool | None:
    source = made.create("root", root)
    destination_rel = f"{PROBE_DIR}/{new_probe_name()}"
    destination = root.open_file(destination_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    made.mine.append(("root", destination_rel))
    source_fd = root.open_file(source, os.O_RDONLY)
    try:
        fcntl.ioctl(destination, FICLONE, source_fd)
    except OSError as error:
        return False if error.errno in NOT_SUPPORTED else None
    finally:
        os.close(source_fd)
        os.close(destination)
    return True


def _inodes(root: ConfinedRoot, made: _Made) -> bool:
    """Does the inode stay the same across a rename and between `stat` and `fstat`?"""
    first = made.create("root", root)
    before = root.stat(first)
    fd = root.open_file(first, os.O_RDONLY)
    try:
        opened = os.fstat(fd)
    finally:
        os.close(fd)
    second = f"{PROBE_DIR}/{new_probe_name()}"
    root.rename(first, second, replace=False, durable=False)  # no-clobber: a collision raises
    made.mine.remove(("root", first))
    made.mine.append(("root", second))
    after = root.stat(second)
    return before.st_ino != 0 and before.st_ino == opened.st_ino == after.st_ino


def _hardlink(root: ConfinedRoot, store: ConfinedRoot | None, made: _Made) -> Link:
    if store is None:
        return Link.NOT_APPLICABLE
    source = made.create("root", root)
    name = f"{PROBE_DIR}/{new_probe_name()}"
    try:
        root.link_to(store, source, name)  # never clobbers
    except OSError as error:
        if error.errno in LINK_REFUSED:
            return Link.NO
        raise
    made.mine.append(("store", name))
    return Link.YES


def probe_root(
    root: ConfinedRoot,
    *,
    store: ConfinedRoot | None,
    filesystem: str,
    now: str,
    remove: Remove,
    store_reason: str = "",
) -> RootCapabilities:
    """Probe a writable root (the caller holds its lease). `store`: the writable `/store` root, or
    None when there is none (M1a), in which case the link probe is not applicable. The files sit
    under `<root>/.shelfhold/staging/` (and the same folder of `/store`) and go through `remove`,
    the closed `probe_file_cleanup` emitter."""
    made = _Made(remove)
    answers: dict[str, Any] = {"reflink": None, "inodes": None, "link": Link.NOT_APPLICABLE}
    try:
        try:
            answers["reflink"] = _reflink(root, made)
        except OSError:
            answers["reflink"] = None  # unknown, never a guess
        try:
            answers["inodes"] = _inodes(root, made)
        except OSError:
            answers["inodes"] = None
        try:
            answers["link"] = _hardlink(root, store, made)
        except OSError:
            answers["link"] = Link.NOT_APPLICABLE if store is None else Link.NO
    finally:
        made.clean()
    return RootCapabilities(
        root=root.path, reflink=answers["reflink"], hardlink_store=answers["link"],
        inodes_stable=answers["inodes"], filesystem=filesystem, probed_at=now,
        store_reason=store_reason if answers["link"] is Link.NOT_APPLICABLE else "",
    )  # fmt: skip


def unprobed(root: str, why: str) -> RootCapabilities:
    return RootCapabilities(root, None, Link.NOT_APPLICABLE, None, unprobed=why)
