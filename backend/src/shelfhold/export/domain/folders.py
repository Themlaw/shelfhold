"""One rule for "do two folders overlap": the one `check_binding` (at the creation of a binding)
and the executor of plans (right before it writes) both apply (ADR-0052, M4 L04).

Components, never string prefixes (`Manga` is not inside `Man`); a name that only differs by case,
Unicode form, a trailing dot or a trailing space is a **lookalike**, refused as an overlap: on a
file system that is lax about names it is the same folder, on a strict one it is a neighbour that
nobody wants. Two folders that are one on the disk (same `(st_dev, st_ino)`, or an ancestor
inode) are **linked** whatever their paths say. Pure: the caller hands over what it saw.
"""

import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from typing import Final

__all__ = ["FOLDER_TRANSPORTS", "Relation", "fold", "folder_relation"]

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
