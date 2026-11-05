"""Rules about shelves and their destination bindings, before anything is planned.

- EF-EXP-002 (D1): in M1a only a shelf whose ceiling is 18+ (or X, or none) can exist;
- EF-EXP-058: `egress` is attested per instance, two `tree` bindings may not overlap, a shelf kept
  in passthrough is linked to a `pull` destination only if a readable copy exists.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from shelfhold.export.domain.path_map import clean_absolute, within

ADULT_MIN_AGE = 18
Egress = Literal["foyer", "externe"]


class BindingError(ValueError):
    """A refusal with a stable catalog code."""

    code = "export.binding"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ShelfBelowEighteenError(BindingError):
    """The parental control arrives in M1b: no shelf below 18+ before then."""

    code = "shelf.parental_control_unavailable"


class TreeOverlapError(BindingError):
    """Two `tree` bindings whose roots contain one another (the "tree and indexers" ADR comes
    before M4)."""

    code = "target.tree_overlap"


class PullNeedsReadableCopyError(BindingError):
    """A `pull` destination serves bytes the core must still be able to read."""

    code = "target.pull_needs_readable_copy"


def check_shelf_ceiling(max_age_rating: int | None) -> None:
    """`None` is a shelf without ceiling (adult); X is stored above every age."""
    if max_age_rating is not None and max_age_rating < ADULT_MIN_AGE:
        raise ShelfBelowEighteenError(f"ceiling {max_age_rating}: available with M1b")


def resolve_egress(*, plugin_default: Egress, attested: Egress | None) -> Egress:
    """What the instance says wins; the plugin's value is only the default (GAP-7-10)."""
    return attested if attested is not None else plugin_default


@dataclass(frozen=True)
class BindingFacts:
    shelf_destination_id: str
    shelf_id: str
    target_id: str
    transport: str
    egress: str
    local_root: str | None  # the folder written, for the transports that have one


def check_binding(
    new: BindingFacts, existing: Iterable[BindingFacts], *, retention_mode: str
) -> None:
    """Refuse a binding that overlaps a tree binding or has no readable copy behind it.

    `existing` are the other bindings of the instance. `retention_mode` is the shelf's.
    """
    others = [b for b in existing if b.shelf_destination_id != new.shelf_destination_id]
    if new.transport == "tree" and new.local_root is not None:
        for other in others:
            if other.transport != "tree" or other.local_root is None:
                continue
            mine, theirs = clean_absolute(new.local_root), clean_absolute(other.local_root)
            if within(mine, theirs) is not None or within(theirs, mine) is not None:
                raise TreeOverlapError(f"{mine} and {theirs} overlap")
    if new.transport == "pull" and retention_mode == "passthrough":
        colinked_tree = any(b.shelf_id == new.shelf_id and b.transport == "tree" for b in others)
        if not colinked_tree:
            raise PullNeedsReadableCopyError("no kept copy and no co-linked tree destination")
