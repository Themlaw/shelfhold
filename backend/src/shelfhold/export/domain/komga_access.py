"""Who can read a Komga library, and which release is trusted (EF-EXP-047, ENF-SEC-016, L08b).

Pure rules; the reading of Komga and the pausing of shelves belong to the adapter. Komga keeps
accounts apart **by library** (a library is shared with an account, or with all of them) and
restricts an account by age. The partitioning of a Komga is therefore granted only when this audit
proves it for the folder of a shelf, never from what the server declares:

- the release is known and at least 1.22.0 (below it, a `known_bad_versions` row of category
  `cve`; a version nobody could read is no better);
- a library over the folder of the shelf holds that folder and nothing else;
- no account that is a child's (non-administrator, with an age restriction) reaches a library
  whose shelves go above what that account may read; a shelf of 18+ (or without a ceiling) is
  reached by no child at all.

An account without an age restriction is a member, not a child: it may read a family library.
Komga names the restriction by the age it hides from (`ALLOW_ONLY`: reads up to `age`; `EXCLUDE`:
hides `age` and above, so reads up to `age - 1`, the stricter of the two readings).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from shelfhold.export.domain.bindings import is_family
from shelfhold.export.domain.kavita_compat import KavitaVersion, KnownBad
from shelfhold.export.domain.path_map import PathMapError, clean_absolute

KOMGA_FLOOR: Final = KavitaVersion((1, 22, 0, 0))
# The advisory number is ours until the project gives one (as for the access-control row of
# Kavita): nothing here claims a CVE identifier that was not checked against its source.
KNOWN_BAD_VERSIONS: Final[tuple[KnownBad, ...]] = (
    KnownBad("komga-below-1.22.0", "cve", below=KOMGA_FLOOR),
)

VERSION_UNKNOWN: Final = "version_unknown"
VERSION_BELOW_FLOOR: Final = "version_below_floor"
LIBRARY_ABOVE_RATING: Final = "library_above_rating"
LIBRARY_NOT_DEDICATED: Final = "library_not_dedicated"
LIBRARY_UNMAPPED: Final = "library_unmapped"
AUDIT_FAILED: Final = "audit_failed"
NEVER_AUDITED: Final = "never_audited"
FOLDER_NOT_AUDITED: Final = "folder_not_audited"

NO_CEILING: Final = 10**6  # a proof "for a shelf without ceiling" covers every ceiling


@dataclass(frozen=True)
class VersionVerdict:
    accepted: bool
    reason: str | None  # a closed code when refused
    known_bad: tuple[KnownBad, ...] = ()


def assess_version(raw: str | None) -> VersionVerdict:
    version = KavitaVersion.parse(raw)
    if version is None:
        return VersionVerdict(False, VERSION_UNKNOWN)
    bad = tuple(k for k in KNOWN_BAD_VERSIONS if k.affects(version))
    return VersionVerdict(not bad, VERSION_BELOW_FLOOR if bad else None, bad)


@dataclass(frozen=True)
class KomgaAccount:
    username: str  # never stored nor shown: it only tells accounts apart during the comparison
    is_admin: bool
    shared_all: bool
    library_ids: frozenset[str]
    age: int | None  # the age this account may read up to; None when it has no restriction

    @property
    def child_like(self) -> bool:
        return not self.is_admin and self.age is not None

    def reaches(self, library_ref: str) -> bool:
        return self.is_admin or self.shared_all or library_ref in self.library_ids


@dataclass(frozen=True)
class KomgaLibrary:
    ref: str
    root: str


@dataclass(frozen=True)
class BoundShelf:
    """A shelf as Komga must see it: the remote folder it fills and its ceiling. `shelf_id` is
    None for the shelf a link is about to be made for."""

    shelf_id: str | None
    root: str | None
    ceiling: int | None


@dataclass(frozen=True)
class Finding:
    code: str
    library: str | None = None  # the reference of the library of Komga, when the finding has one
    shelf_id: str | None = None


def restriction_age(age: int, restriction: str) -> int:
    """The age an account reads up to. Raises ValueError for a restriction nobody knows."""
    if restriction == "ALLOW_ONLY":
        return age
    if restriction == "EXCLUDE":
        return max(age - 1, 0)
    raise ValueError("unknown age restriction")


def _clean(path: str) -> str | None:
    try:
        return clean_absolute(path)
    except PathMapError:
        return None


def _under(root: str, folder: str) -> bool:
    base = [p for p in root.split("/") if p]
    parts = [p for p in folder.split("/") if p]
    return parts[: len(base)] == base and len(parts) >= len(base)


def _library_ceiling(ceilings: Iterable[int | None]) -> int | None:
    seen = list(ceilings)
    if not seen or any(c is None for c in seen):
        return None
    return max(c for c in seen if c is not None)


def evaluate(
    accounts: Sequence[KomgaAccount],
    libraries: Sequence[KomgaLibrary],
    shelves: Sequence[BoundShelf],
) -> tuple[Finding, ...]:
    """Every gap found, by shelf. No finding for a shelf means the partitioning is proved for it."""
    cleaned = {s: _clean(s.root) if s.root else None for s in shelves}
    covering: dict[BoundShelf, list[KomgaLibrary]] = {}
    for shelf in shelves:
        root = cleaned[shelf]
        covering[shelf] = [
            lib for lib in libraries if root is not None and (r := _clean(lib.root)) is not None
            and _under(r, root)
        ]  # fmt: skip
    findings: list[Finding] = []
    holds_more: set[str] = set()  # libraries that hold more than the folder of one of their shelves
    for shelf in shelves:
        if not covering[shelf]:
            findings.append(Finding(LIBRARY_UNMAPPED, None, shelf.shelf_id))
        for lib in covering[shelf]:
            if _clean(lib.root) != cleaned[shelf]:
                holds_more.add(lib.ref)
                findings.append(Finding(LIBRARY_NOT_DEDICATED, lib.ref, shelf.shelf_id))
    ceilings: dict[str, list[int | None]] = {}
    for shelf in shelves:
        for lib in covering[shelf]:
            ceilings.setdefault(lib.ref, []).append(
                None if lib.ref in holds_more else shelf.ceiling
            )
    for shelf in shelves:
        for lib in covering[shelf]:
            ceiling = _library_ceiling(ceilings[lib.ref])
            for account in accounts:
                if not account.child_like or not account.reaches(lib.ref):
                    continue
                family = ceiling is not None and is_family(ceiling)
                if not family or ceiling > (account.age or 0):  # type: ignore[operator]
                    findings.append(Finding(LIBRARY_ABOVE_RATING, lib.ref, shelf.shelf_id))
                    break
    return tuple(dict.fromkeys(findings))
