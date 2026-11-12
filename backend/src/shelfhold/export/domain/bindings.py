"""Rules about shelves and their destination bindings, before anything is planned.

- EF-EXP-002: a ceiling is a tier of the scale (or X, or none); a shelf below 18+ is a family
  shelf (ADR-0027);
- EF-EXP-047: a family shelf is linked only to a destination that partitions access by library or
  by user; what a third-party plugin declares is a default the admin attests;
- EF-EXP-058: `egress` is attested per instance, two `tree` bindings may not overlap, a shelf kept
  in passthrough is linked to a `pull` destination only if a readable copy exists;
- EF-BIB-037: two shelves never share a remote folder of the same server.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from shelfhold.export.domain.path_map import clean_absolute, within
from shelfhold.sdk.age import AGE_TIERS

ADULT_MIN_AGE = 18
X_CEILING = 99  # `shelf.max_age_rating` stores X as 99
FAMILY_SHELF_NAME = "Famille"  # the pre-created shelf for children (EF-UX-007)
FAMILY_SHELF_MAX_AGE = 13  # its ceiling can never be above 13+
PARTITIONED = ("library", "user")
Egress = Literal["foyer", "externe"]


class BindingError(ValueError):
    """A refusal with a stable catalog code."""

    code = "export.binding"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ShelfCeilingError(BindingError):
    """A ceiling that is no tier of the scale (nor X, nor none)."""

    code = "shelf.ceiling_invalid"


class FamilyCeilingTooHighError(BindingError):
    """The shelf "Famille" is capped at 13+: no ceiling above it, none at all, and not X."""

    code = "shelf.family_ceiling_too_high"


class UnpartitionedDestinationError(BindingError):
    """A shelf below 18+ needs a destination that keeps children out of the rest (EF-EXP-047)."""

    code = "shelf.destination_not_partitioned"


class LibraryNotDedicatedError(BindingError):
    """The Kavita library a family shelf would use also holds files of something else."""

    code = "shelf.library_not_dedicated"


class RemoteFolderSharedError(BindingError):
    """Two shelves would write the same folder of the same server."""

    code = "target.remote_folder_shared"


class TreeOverlapError(BindingError):
    """Two `tree` bindings whose roots contain one another (the "tree and indexers" ADR comes
    before M4)."""

    code = "target.tree_overlap"


class ShelfAlreadyBoundError(BindingError):
    """M1a: one destination per shelf (EF-EXP-002); several come with M1b."""

    code = "shelf.destination_exists"


class PullNeedsReadableCopyError(BindingError):
    """A `pull` destination serves bytes the core must still be able to read."""

    code = "target.pull_needs_readable_copy"


def is_family(max_age_rating: int | None) -> bool:
    """A shelf below 18+: the barrier of the parental control applies to it."""
    return max_age_rating is not None and max_age_rating < ADULT_MIN_AGE


def check_ceiling(max_age_rating: int | None) -> None:
    """`None` is a shelf without ceiling; 99 is X; any other value is a tier of the scale."""
    if max_age_rating is None or max_age_rating == X_CEILING or max_age_rating in AGE_TIERS:
        return
    raise ShelfCeilingError(f"ceiling {max_age_rating} is no tier of {AGE_TIERS}")


def check_family_cap(name: str, max_age_rating: int | None) -> None:
    """ "Famille" never goes above 13+: refused at creation and at every change of its ceiling.
    `None` (no ceiling), 99 (X) and any tier above 13 are refused."""
    if name != FAMILY_SHELF_NAME:
        return
    if max_age_rating is None or max_age_rating > FAMILY_SHELF_MAX_AGE:
        raise FamilyCeilingTooHighError(
            f"shelf {FAMILY_SHELF_NAME!r} is capped at {FAMILY_SHELF_MAX_AGE}+, "
            f"not {max_age_rating}"
        )


def effective_partitioning(kind: str, stored: str, *, attested: bool) -> str:
    """How a destination keeps accounts apart. Kavita does it by library (it is the adapter that
    knows); the value a third-party plugin declares is a default proposal and counts only once
    the admin attested it (K9, GAP-10-08); an unattested one is `none`."""
    if kind == "kavita":
        return "library"
    return stored if attested else "none"


def check_partitioning(max_age_rating: int | None, partitioning: str) -> None:
    if is_family(max_age_rating) and partitioning not in PARTITIONED:
        raise UnpartitionedDestinationError(
            f"a shelf below 18+ needs a destination with access partitioning, not {partitioning!r}"
        )


def resolve_egress(*, plugin_default: Egress, attested: Egress | None) -> Egress:
    """What the instance says wins; the plugin's value is only the default (GAP-7-10)."""
    return attested if attested is not None else plugin_default


WRITING_TRANSPORTS = ("tree", "dropbox", "push")  # the ones that put files in a local folder


@dataclass(frozen=True)
class BindingFacts:
    shelf_destination_id: str
    shelf_id: str
    target_id: str
    transport: str
    egress: str
    local_root: str | None  # the folder written (real path), for the transports that have one
    identity: tuple[int, int] | None = None  # (st_dev, st_ino) of that folder when it exists
    remote_root: str | None = None  # the same folder as the server sees it


def check_binding(
    new: BindingFacts, existing: Iterable[BindingFacts], *, retention_mode: str
) -> None:
    """Refuse a second destination for a shelf, a binding whose folder overlaps another writing
    one (same real path or same device and inode), or a pull with no readable copy behind it.

    `existing` are the other bindings of the instance. `retention_mode` is the shelf's.
    """
    others = [b for b in existing if b.shelf_destination_id != new.shelf_destination_id]
    if any(b.shelf_id == new.shelf_id for b in others):
        raise ShelfAlreadyBoundError(f"shelf {new.shelf_id} already has a destination")
    if new.transport in WRITING_TRANSPORTS and new.local_root is not None:
        for other in others:
            if other.transport not in WRITING_TRANSPORTS or other.local_root is None:
                continue
            mine, theirs = clean_absolute(new.local_root), clean_absolute(other.local_root)
            same_folder = new.identity is not None and new.identity == other.identity
            if same_folder or within(mine, theirs) is not None or within(theirs, mine) is not None:
                raise TreeOverlapError(f"{mine} and {theirs} overlap")
    if new.remote_root is not None:
        mine = clean_absolute(new.remote_root)
        for other in others:
            if other.target_id != new.target_id or other.remote_root is None:
                continue
            theirs = clean_absolute(other.remote_root)
            if within(mine, theirs) is not None or within(theirs, mine) is not None:
                raise RemoteFolderSharedError(f"{mine} and {theirs} are one folder of the server")
    if new.transport == "pull" and retention_mode == "passthrough":
        colinked_tree = any(b.shelf_id == new.shelf_id and b.transport == "tree" for b in others)
        if not colinked_tree:
            raise PullNeedsReadableCopyError("no kept copy and no co-linked tree destination")
