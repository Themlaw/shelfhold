"""One rule for "do two folders overlap": the one `check_binding` (at the creation of a binding)
and the executor of plans (right before it writes) both apply (ADR-0052, M4 L04).

Components, never string prefixes (`Manga` is not inside `Man`); a name that only differs by case,
Unicode form, a trailing dot or a trailing space is a **lookalike**, refused as an overlap: on a
file system that is lax about names it is the same folder, on a strict one it is a neighbour that
nobody wants. Two folders that are one on the disk (same `(st_dev, st_ino)`, or an ancestor
inode) are **linked** whatever their paths say. Pure: the caller hands over what it saw.
"""

import unicodedata
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Final

__all__ = ["FOLDER_TRANSPORTS", "Relation", "absolute_relation", "fold", "folder_relation"]

# the transports that put files in a folder of the host: only they can overlap
FOLDER_TRANSPORTS: Final = ("tree", "dropbox", "push")


class Relation(StrEnum):
    APART = "apart"
    SAME = "same"  # exactly the same folder: the identity of T1
    INSIDE = "inside"  # the first is below the second
    CONTAINS = "contains"  # the first holds the second
    LOOKALIKE = "lookalike"  # the same folder under another case or Unicode form
    LINKED = "linked"  # one folder through an inode, a mount or an ancestor


def fold(component: str) -> str:
    """What a lax file system makes of a name: Unicode form, case, trailing dots and spaces."""
    text = unicodedata.normalize("NFKC", component)
    return text.casefold().rstrip(". ")


def _is_prefix(shorter: Sequence[str], longer: Sequence[str]) -> bool:
    return len(shorter) < len(longer) and tuple(longer[: len(shorter)]) == tuple(shorter)


def folder_relation(a: Sequence[str], b: Sequence[str], *, linked: bool = False) -> Relation:
    """How the folder of components `a` stands to the one of `b`, below the same root."""
    if tuple(a) == tuple(b):
        return Relation.SAME
    if _is_prefix(b, a):
        return Relation.INSIDE
    if _is_prefix(a, b):
        return Relation.CONTAINS
    fa, fb = tuple(map(fold, a)), tuple(map(fold, b))
    if fa == fb or _is_prefix(fa, fb) or _is_prefix(fb, fa):
        return Relation.LOOKALIKE
    return Relation.LINKED if linked else Relation.APART


def absolute_relation(
    a: str,
    b: str,
    *,
    identity_a: tuple[int, int] | None = None,
    ancestry_a: Iterable[tuple[int, int]] = (),
    identity_b: tuple[int, int] | None = None,
    ancestry_b: Iterable[tuple[int, int]] = (),
) -> Relation:
    """The same rule for two absolute paths that are already real (links resolved, no `..`): by
    components, then by what the disk says. `(st_dev, st_ino)` of a folder, and of every folder
    from the top down to it (`ancestry`, itself included), make two folders that no string relates
    one: a bind mount, a case-insensitive twin, a folder reached through two roots."""
    parts_a = tuple(p for p in a.split("/") if p)
    parts_b = tuple(p for p in b.split("/") if p)
    ancestry_a, ancestry_b = frozenset(ancestry_a), frozenset(ancestry_b)
    linked = identity_a is not None and identity_b is not None
    linked = linked and (
        identity_a == identity_b or identity_a in ancestry_b or identity_b in ancestry_a
    )
    return folder_relation(parts_a, parts_b, linked=linked)
