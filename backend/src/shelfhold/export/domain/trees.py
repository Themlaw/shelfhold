"""One tree, several indexers: when two folders are one tree and when they overlap (ADR-0052 T1).

A tree is a folder Shelfhold writes, identified by `(physical root, normalized root_subpath)`.
That pair is the only identity that is stored. Two requests for **exactly** that pair name the
same tree and attach only on the admin's explicit choice (`attach_to`). Anything else that
touches a tree is an overlap and stays refused with `target.tree_overlap`: a folder inside a tree,
a folder that holds one (compared by components, never by string prefix), the same folder under
another case or another Unicode form, a folder whose `(st_dev, st_ino)` is the one of a tree or
of one of its ancestors or descendants. The inode only serves this check, it is never stored
(`st_dev` changes from one boot to the next).

Pure rules, no disk: the caller hands over what it saw (`TreeFolder.identity`, `.ancestry`).
"""

import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from shelfhold.export.domain.bindings import TreeOverlapError
from shelfhold.export.domain.folders import Relation, fold, folder_relation

__all__ = [
    "Relation",
    "TreeFolder",
    "TreePathError",
    "check_no_symlink",
    "check_tree_attachment",
    "fold",
    "normalize_subpath",
    "relation",
]

MAX_SUBPATH_BYTES: Final = 4096


class TreePathError(ValueError):
    """A `root_subpath` that is not a folder name below the root."""


def normalize_subpath(raw: str) -> str:
    """The subpath as stored: components joined by `/`, none empty, none `.` or `..`, no
    backslash, no control character, no leading or trailing `/`. '' is the root itself. A path
    that climbs is refused, never resolved away."""
    if not isinstance(raw, str):
        raise TreePathError("a subpath is a string")
    if len(raw.encode("utf-8", "surrogatepass")) > MAX_SUBPATH_BYTES:
        raise TreePathError("subpath too long")
    if "\\" in raw or any(unicodedata.category(ch) in ("Cc", "Cf") for ch in raw):
        raise TreePathError("subpath has a backslash or a control character")
    if raw.startswith("/") or raw.endswith("/") or (raw != "" and not raw.strip("/")):
        raise TreePathError("subpath is relative to its root, with no leading or trailing slash")
    parts = raw.split("/") if raw else []
    if any(part in ("", ".", "..") for part in parts):
        raise TreePathError("subpath has an empty, `.` or `..` component")
    return "/".join(parts)


@dataclass(frozen=True)
class TreeFolder:
    """A folder that is, or would be, a tree. `tree_id` is None for a candidate."""

    root_id: str
    subpath: str
    tree_id: str | None = None
    identity: tuple[int, int] | None = None  # (st_dev, st_ino) of the folder, when it exists
    # (st_dev, st_ino) of every folder from the root down to this one, itself included
    ancestry: frozenset[tuple[int, int]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if normalize_subpath(self.subpath) != self.subpath:
            raise TreePathError("a tree folder carries a normalized subpath")

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.subpath.split("/")) if self.subpath else ()


def relation(a: TreeFolder, b: TreeFolder) -> Relation:
    """How `a` stands to `b`: the rule of `folders.folder_relation`, plus the disk."""
    linked = a.identity is not None and b.identity is not None
    linked = linked and (
        a.identity == b.identity or a.identity in b.ancestry or b.identity in a.ancestry
    )
    if a.root_id != b.root_id:
        return Relation.LINKED if linked else Relation.APART
    return folder_relation(a.parts, b.parts, linked=linked)


def check_tree_attachment(
    candidate: TreeFolder, existing: Iterable[TreeFolder], *, attach_to: str | None = None
) -> TreeFolder | None:
    """What a binding on `candidate` does with the trees that exist.

    Returns None when the folder is a new tree, or the existing tree it attaches to. Raises
    `TreeOverlapError` (`target.tree_overlap`) for every overlap that is not the exact identity of
    a declared tree, and for the exact identity itself unless `attach_to` names that tree."""
    attached: TreeFolder | None = None
    for tree in existing:
        if tree.tree_id is not None and tree.tree_id == candidate.tree_id:
            continue  # the tree is not its own neighbour
        how = relation(candidate, tree)
        if how is Relation.APART:
            continue
        if how is Relation.SAME:
            if attach_to is None or attach_to != tree.tree_id:
                raise TreeOverlapError(f"{tree.subpath or '/'} is already a tree: attach to it")
            attached = tree
            continue
        raise TreeOverlapError(
            f"{candidate.subpath or '/'} overlaps the tree {tree.subpath or '/'}"
        )
    if attach_to is not None and attached is None:
        raise TreeOverlapError("the tree to attach to is not this folder")
    return attached


def check_no_symlink(subpath: str, kind_of: Callable[[str], str | None]) -> None:
    """Refuse a folder whose path crosses a link (`RESOLVE_NO_SYMLINKS`, EF-EXP-006). `kind_of`
    looks at one folder, link not followed: 'symlink' when it is one, None when absent."""
    parts = subpath.split("/") if subpath else []
    for depth in range(1, len(parts) + 1):
        if kind_of("/".join(parts[:depth])) == "symlink":
            raise TreePathError("a tree folder never goes through a symbolic link")
