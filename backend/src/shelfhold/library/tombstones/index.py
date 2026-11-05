"""The membership index: decrypted tombstones, in memory only (EF-BIB-054 (2), I46).

Never persisted, never logged (no `__repr__` of its content). A candidate is compared with each
tombstone of its scope after re-normalising the raw values of both sides, under the current
version of each normalizer **and** every version a tombstone cites; a file is compared through
each `content_hash` algorithm a tombstone cites.

Which field goes through which normalizer is fixed here (`NORMALIZED_FIELDS`); every other field
is compared exactly, as received. An Edition key is compared on the columns the tombstone holds:
a column added to the key later (with its sentinel value on old rows) cannot free a purged place.
When in doubt the index blocks: re-acquiring purged content is the failure that matters.
"""

import contextlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import product
from typing import Final, Protocol

from shelfhold.library.tombstones.values import (
    ContentHash,
    PlaceRef,
    Scope,
    SeriesBindingRef,
    TombstoneValues,
    VariantRef,
    WorkRef,
)

# field of the flattened values -> name of its normalizer (ENF-OPS-021)
NORMALIZED_FIELDS: Final[Mapping[str, str]] = {
    "number_raw": "number",
    # an output of the number normalizer: re-derived from `number_raw`, never compared as stored
    "number_segment": "number_segment",
    "edition_key.lang": "lang",
    "edition_key.publisher": "publisher",
    "edition_key.text_version": "text_version",
}
# An empty number (unnumbered) says nothing; an empty segment or publisher is a real value.
EMPTY_MEANS_UNKNOWN: Final = frozenset({"number_raw"})
HashWith = Callable[[str], str | None]


class Normalizers(Protocol):
    """What the index needs from the registry of versioned normalizers (catalog)."""

    def current_version(self, name: str) -> str:
        """`name@version` of the current version."""
        ...

    def known(self, normalizer: str) -> bool:
        """Whether `name@version` is still in the code."""
        ...

    def apply(self, normalizer: str, raw: str) -> str:
        """Normalise under exactly `name@version`; `ValueError` when it refuses the value."""
        ...


class CitedVersionMissingError(RuntimeError):
    """A normalizer version cited by a tombstone is no longer in the code (ENF-OPS-021).

    The index refuses to build rather than compare under the wrong version: the CI check
    `test_cited_normalizer_version_never_removed` exists so that this never happens."""

    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(sorted(set(missing)))
        super().__init__(f"normalizer versions cited by tombstones are missing: {self.missing}")


def flatten(values: TombstoneValues) -> dict[str, str]:
    if isinstance(values, VariantRef):
        return {
            "source_key": values.source_key,
            "sub_source": values.sub_source,
            "remote_unit_id": values.remote_unit_id,
        }
    if isinstance(values, PlaceRef):
        flat = {f"edition_key.{k}": v for k, v in values.edition_key}
        flat.update(
            track=values.track,
            number_segment=values.number_raw,  # derived again under each version (see above)
            number_raw=values.number_raw,
            part=values.part,
        )
        return flat
    if isinstance(values, SeriesBindingRef):
        return {
            "source_key": values.source_key,
            "sub_source": values.sub_source,
            "series_ref": values.series_ref,
        }
    if isinstance(values, WorkRef):
        return {"id_scheme": values.id_scheme, "value": values.value}
    raise TypeError(type(values).__name__)


def _hashes(values: TombstoneValues) -> tuple[ContentHash, ...]:
    if isinstance(values, VariantRef):
        return values.content_hashes + values.derived_hashes
    return ()


# A group shares the scope, the set of fields and one choice of version per normalized field.
_Group = tuple[Scope, frozenset[str], tuple[tuple[str, str], ...]]
_Key = tuple[tuple[str, str], ...]


class MembershipIndex:
    def __init__(self, normalizers: Normalizers) -> None:
        self._normalizers = normalizers
        self._entries: list[tuple[Scope, dict[str, str], dict[str, str]]] = []
        self._hashes: set[tuple[str, str]] = set()
        self._groups: dict[_Group, set[_Key]] = {}
        self._versions: dict[str, set[str]] = {}

    def __repr__(self) -> str:
        return f"MembershipIndex({len(self._entries)} tombstones)"

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def cited_hash_algorithms(self) -> frozenset[str]:
        return frozenset(by for by, _ in self._hashes)

    def cited_versions(self) -> dict[str, frozenset[str]]:
        return {name: frozenset(v) for name, v in self._versions.items()}

    def add_all(self, items: Iterable[tuple[TombstoneValues, Mapping[str, str]]]) -> None:
        """Add tombstones and rebuild the groups (a new cited version re-keys every entry)."""
        added = list(items)
        missing: list[str] = []
        for values, cited in added:
            for name, version in cited.items():
                if name in NORMALIZED_FIELDS.values():
                    self._versions.setdefault(name, set()).add(version)
            self._entries.append((values.scope, flatten(values), dict(cited)))
            self._hashes.update((h.by, h.value) for h in _hashes(values))
        for versions in self._versions.values():
            missing.extend(v for v in versions if not self._normalizers.known(v))
        if missing:
            raise CitedVersionMissingError(missing)
        self._rebuild()

    def _versions_of(self, name: str) -> list[str]:
        versions = set(self._versions.get(name, ()))
        with contextlib.suppress(KeyError):  # a normalizer that only tombstones still cite
            versions.add(self._normalizers.current_version(name))
        return sorted(versions)

    def _form(self, field_name: str, version: str, raw: str) -> str:
        try:
            normalized = self._normalizers.apply(version, raw)
        except ValueError:
            return "r:" + raw  # refused by this version: compared as received
        # An empty form (an unnumbered chapter) says nothing about the place: compare the raw
        # value, or every unnumbered chapter of the Edition would collide with a purged one.
        if not normalized and field_name in EMPTY_MEANS_UNKNOWN:
            return "r:" + raw
        return "n:" + normalized

    def _assignments(self, fields: frozenset[str]) -> list[tuple[tuple[str, str], ...]]:
        normalized = sorted(f for f in fields if f in NORMALIZED_FIELDS)
        choices = [self._versions_of(NORMALIZED_FIELDS[f]) for f in normalized]
        return [tuple(zip(normalized, combo, strict=True)) for combo in product(*choices)]

    def _key(self, flat: Mapping[str, str], assignment: tuple[tuple[str, str], ...]) -> _Key:
        chosen = dict(assignment)
        return tuple(
            (name, self._form(name, chosen[name], value) if name in chosen else "r:" + value)
            for name, value in sorted(flat.items())
        )

    def _rebuild(self) -> None:
        self._groups = {}
        for scope, flat, _cited in self._entries:
            fields = frozenset(flat)
            for assignment in self._assignments(fields):
                group = (scope, fields, assignment)
                self._groups.setdefault(group, set()).add(self._key(flat, assignment))

    def contains_values(self, candidate: TombstoneValues) -> bool:
        """The candidate's own values match a tombstone of its scope (hashes left out)."""
        flat = flatten(candidate)
        for (scope, fields, assignment), keys in self._groups.items():
            if scope != candidate.scope or not fields <= flat.keys():
                continue
            projected = {name: flat[name] for name in fields}
            if self._key(projected, assignment) in keys:
                return True
        return False

    def contains(self, candidate: TombstoneValues, *, hash_with: HashWith | None = None) -> bool:
        """True when the candidate matches a tombstone of its scope."""
        return self.contains_values(candidate) or self.contains_content(
            _hashes(candidate), hash_with=hash_with
        )

    def contains_content(
        self, hashes: Iterable[ContentHash] = (), *, hash_with: HashWith | None = None
    ) -> bool:
        """A file, by its content alone: the hashes it comes with, then the file hashed again
        with each algorithm a tombstone cites (`hash_with(by)`, None when it cannot)."""
        if {(h.by, h.value) for h in hashes} & self._hashes:
            return True
        if hash_with is not None:
            for by in sorted(self.cited_hash_algorithms):
                value = hash_with(by)
                if value is not None and (by, value) in self._hashes:
                    return True
        return False


@dataclass(frozen=True)
class RegistryNormalizers:
    """`Normalizers` over the catalog's `NormalizerRegistry` (or anything with its shape)."""

    current: Callable[[str], str]
    function_of: Callable[[str], Callable[[str], str]]

    def current_version(self, name: str) -> str:
        return self.current(name)

    def known(self, normalizer: str) -> bool:
        try:
            self.function_of(normalizer)
        except (KeyError, ValueError):
            return False
        return True

    def apply(self, normalizer: str, raw: str) -> str:
        return self.function_of(normalizer)(raw)
