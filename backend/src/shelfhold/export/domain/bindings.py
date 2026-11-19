"""Rules about shelves and their destination bindings, before anything is planned.

- EF-EXP-002: a shelf has one binding per folder and server (fan-out); a ceiling is a tier of
  the scale (or X, or none); a shelf below 18+ is a family shelf (ADR-0027);
- EF-EXP-047: a family shelf is linked only to a destination that partitions access by library or
  by user; what a third-party plugin declares is a default the admin attests;
- EF-EXP-058: `egress` is attested per instance, two `tree` bindings may not overlap, a shelf kept
  in passthrough is linked to a `pull` destination only if a readable copy exists;
- EF-BIB-037: two shelves never share a remote folder of the same server.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from shelfhold.export.domain.folders import FOLDER_TRANSPORTS, Relation, folder_relation
from shelfhold.export.domain.path_map import clean_absolute, within
from shelfhold.sdk.age import ADULT_MIN_AGE, AGE_TIERS, is_family_ceiling

X_CEILING = 99  # `shelf.max_age_rating` stores X as 99
FAMILY_SHELF_NAME = "Famille"  # the pre-created shelf for children (EF-UX-007)
FAMILY_SHELF_MAX_AGE = 13  # its ceiling can never be above 13+
PARTITIONED = ("library", "user")
Egress = Literal["foyer", "externe"]


class BindingError(ValueError):
    """A refusal with a stable catalog code."""

    code = "export.binding"

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(f"{self.code}: {message}")
        self.reason = reason  # a closed code the admin may be shown (never free text)


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


class RootOverlapsLocalSourceError(BindingError):
    """A native folder root that is, holds or sits inside the local source of Suwayomi: Shelfhold
    would read back its own exports as an upstream (EF-EXP-010)."""

    code = "target.root_overlaps_local_source"


class LocalSourceUnknownError(BindingError):
    """A Suwayomi is configured and no follow cycle has read its local source yet: nothing can be
    said about the loop, so no native folder is bound until one has (fail closed)."""

    code = "target.local_source_unknown"


class TargetKindTransportError(BindingError):
    """A first-party target is bound only by the transport it has: a CWA drop box is a `dropbox`
    (EF-EXP-022), whatever the caller says."""

    code = "target.transport_not_supported"


class KomgaPrimaryUnsupportedError(BindingError):
    """A Komga binding that would own a tree: the executor plans no write for one, so the shelf
    would never receive its files. Until M5, Komga is bound as a secondary indexer of a tree
    another binding writes (ADR-0052)."""

    code = "binding.komga_primary_unsupported"


class FolderOutsideRootError(BindingError):
    """The folder of a drop box is the folder its target declares, or one below it: never one that
    climbs out of it (`..`), is given as an absolute path, or goes through a link."""

    code = "target.folder_outside_root"


class ShelfAlreadyBoundError(BindingError):
    """M1a: one destination per shelf. M4 L06a (fan-out) lifts it: a shelf has as many bindings as
    folders and servers; the code stays in the catalogue for the clients that knew it."""

    code = "shelf.destination_exists"


class PullNeedsReadableCopyError(BindingError):
    """A `pull` destination serves bytes the core must still be able to read."""

    code = "target.pull_needs_readable_copy"


def is_family(max_age_rating: int | None) -> bool:
    """A shelf below 18+: the barrier of the parental control applies to it."""
    return is_family_ceiling(max_age_rating)


def family_clause(ceiling: Any) -> Any:
    """`is_family` for a SQL column (`shelf.max_age_rating`): a ceiling below 18+. NULL is not."""
    return ceiling < ADULT_MIN_AGE


def adult_clause(ceiling: Any) -> Any:
    """The complement of `family_clause`: no ceiling, or 18+ and above."""
    return ceiling.is_(None) | (ceiling >= ADULT_MIN_AGE)


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


def effective_partitioning(
    kind: str, stored: str, *, attested: bool, audit_proved: bool = False
) -> str:
    """How a destination keeps accounts apart. Kavita does it by library (it is the adapter that
    knows). Komga does too, but only for a folder whose accounts its audit proved (L08b,
    EF-EXP-047): the declaration and an admin's attestation count for nothing without that proof
    (`audit_proved`, read for the folder of the binding). A CWA drop box is a folder that one
    ingester empties: it partitions nothing, whatever it is declared or attested. The value a
    third-party plugin declares is a default proposal and counts only once the admin attested it
    (K9, GAP-10-08); an unattested one is `none`."""
    if kind == "kavita":
        return "library"
    if kind == "komga":
        return "library" if audit_proved else "none"
    if kind == "cwa":
        return "none"  # a drop box keeps no account apart, and no attestation changes that
    return stored if attested else "none"


def check_dropbox_folder(declared: str, root_subpath: str, real: str | None) -> None:
    """The ingestion folder of a drop box is inside the folder its target declares, by name and
    once the links are followed (`real` is the real path of the folder, None if there is none)."""
    parts = root_subpath.split("/") if root_subpath else []
    if root_subpath.startswith("/") or "\0" in root_subpath or ".." in parts:
        raise FolderOutsideRootError("the folder of a drop box is below the folder of its target")
    if real is not None and within(real, declared) is None:
        raise FolderOutsideRootError("the folder of a drop box leaves its target through a link")


def check_partitioning(
    max_age_rating: int | None, partitioning: str, *, reason: str | None = None
) -> None:
    if is_family(max_age_rating) and partitioning not in PARTITIONED:
        why = f" ({reason})" if reason else ""
        raise UnpartitionedDestinationError(
            f"a shelf below 18+ needs a destination with access partitioning, not "
            f"{partitioning!r}{why}",
            reason=reason,
        )


def resolve_egress(*, plugin_default: Egress, attested: Egress | None) -> Egress:
    """What the instance says wins; the plugin's value is only the default (GAP-7-10)."""
    return attested if attested is not None else plugin_default


WRITING_TRANSPORTS = FOLDER_TRANSPORTS  # the ones that put files in a local folder


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
    """Refuse a binding whose folder overlaps another writing one (same real path or same device
    and inode), or a pull with no readable copy behind it. A shelf takes as many bindings as it
    has folders and servers (EF-EXP-002, fan-out); the barrier of a shelf below 18+ is looked at
    for each of them (`check_partitioning`, and again at every planning).

    `existing` are the other bindings of the instance. `retention_mode` is the shelf's. The caller
    that attaches a server to a tree already written (ADR-0052) leaves that tree's own bindings
    out of `existing`: the identity is the explicit choice, every other overlap stays refused.
    """
    others = [b for b in existing if b.shelf_destination_id != new.shelf_destination_id]
    if new.transport in WRITING_TRANSPORTS and new.local_root is not None:
        for other in others:
            if other.transport not in WRITING_TRANSPORTS or other.local_root is None:
                continue
            mine, theirs = clean_absolute(new.local_root), clean_absolute(other.local_root)
            same_inode = new.identity is not None and new.identity == other.identity
            parts = ([p for p in mine.split("/") if p], [p for p in theirs.split("/") if p])
            if folder_relation(*parts, linked=same_inode) is not Relation.APART:
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
