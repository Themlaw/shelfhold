"""Versioned normalizers (ENF-OPS-021, ENF-I18N-002).

Every identity key is registered under `normalizer_id@version` and stored with the raw value. A
change of behaviour is a new version; rows of an older version stay valid until an explicit
migration plan (with the merge preview) is applied. Nothing is ever recomputed on read:
`resolve_key` returns the stored value.
"""

import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from shelfhold.catalog.domain.numbering import (
    NUMBER_NORMALIZER_NAME,
    NUMBER_NORMALIZER_VERSION,
    parse_number,
)
from shelfhold.catalog.lang import (
    LANG_NORMALIZER_NAME,
    LANG_NORMALIZER_VERSION,
    normalize_lang,
)

NormalizerFn = Callable[[str], str]

REMOTE_UNIT_ID_NORMALIZER_NAME = "remote_unit_id"
REMOTE_UNIT_ID_NORMALIZER_VERSION = 1
REMOTE_UNIT_ID_NORMALIZER_ID = (
    f"{REMOTE_UNIT_ID_NORMALIZER_NAME}@{REMOTE_UNIT_ID_NORMALIZER_VERSION}"
)


def normalize_remote_unit_id(raw: str) -> str:
    """Version 1 keeps the upstream identifier exactly as received (EF-BIB-050, EF-SRC-036).

    The identifier is the identity of a Variant and is never rewritten: a plugin derives a stable
    one, and a later version of this normalizer may only be applied through a migration plan.
    """
    return raw


@dataclass(frozen=True, order=True)
class NormalizerId:
    name: str
    version: int

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def parse(cls, text: str) -> "NormalizerId":
        name, sep, version = text.partition("@")
        if not sep or not name or not version.isdecimal() or int(version) < 1 or "@" in version:
            raise ValueError(f"invalid normalizer id: {text!r}")
        return cls(name, int(version))


@dataclass(frozen=True)
class Normalized:
    """Normalized value, raw value and normalizer, stored together (`*_norm_by`)."""

    value: str
    raw: str
    by: str

    @property
    def key(self) -> str:
        return self.value


def resolve_key(stored: Normalized) -> str:
    """Identity key of a stored row: that of its own version, never recomputed."""
    return stored.value


@dataclass(frozen=True)
class Merge:
    """Preview: raw values that would become a single key after migration."""

    new_value: str
    raws: tuple[str, ...]


@dataclass(frozen=True)
class Unresolved:
    """A row whose raw value the target version refuses: the admin must resolve it."""

    row: Normalized
    reason: str


@dataclass(frozen=True)
class MigrationPlan:
    target: NormalizerId
    changes: tuple[Normalized, ...]  # rows of an older version, to recompute
    results: tuple[Normalized, ...]  # their value under the target version, same order
    merges: tuple[Merge, ...]
    unresolved: tuple[Unresolved, ...] = ()

    def apply(self) -> tuple[Normalized, ...]:
        """The migrated rows; the original rows are not modified, unresolved rows stay as is."""
        return self.results


class NormalizerRegistry:
    def __init__(self) -> None:
        self._functions: dict[NormalizerId, NormalizerFn] = {}
        self._current: dict[str, int] = {}

    def register(self, name: str, version: int, fn: NormalizerFn) -> NormalizerId:
        nid = NormalizerId(name, version)
        if nid in self._functions:
            raise ValueError(f"{nid} already registered")
        if version != self._current.get(name, 0) + 1:
            raise ValueError(f"{nid} skips a version")
        self._functions[nid] = fn
        self._current[name] = version
        return nid

    def names(self) -> list[str]:
        return sorted(self._current)

    def current(self, name: str) -> NormalizerId:
        return NormalizerId(name, self._current[name])

    def get(self, nid: NormalizerId) -> NormalizerFn:
        return self._functions[nid]

    def normalize(self, name: str, raw: str) -> Normalized:
        nid = self.current(name)
        return Normalized(self._functions[nid](raw), raw, str(nid))

    def plan_migration(self, rows: Iterable[Normalized], name: str) -> MigrationPlan:
        """Preview of a migration to the current version of `name`, applying nothing.

        Merges are computed over the migrated rows **and** the rows already at the target version,
        so a collision with an up-to-date row shows too. A raw value the target version refuses
        becomes an `Unresolved` entry instead of failing the whole plan.
        """
        target = self.current(name)
        fn = self._functions[target]
        changes: list[Normalized] = []
        results: list[Normalized] = []
        unresolved: list[Unresolved] = []
        up_to_date: list[Normalized] = []
        for row in rows:
            if NormalizerId.parse(row.by) == target:
                up_to_date.append(row)
                continue
            try:
                results.append(Normalized(fn(row.raw), row.raw, str(target)))
            except ValueError as exc:
                unresolved.append(Unresolved(row, str(exc)))
            else:
                changes.append(row)
        groups: dict[str, set[str]] = defaultdict(set)
        for row in (*results, *up_to_date):
            groups[row.value].add(row.raw)
        migrated_values = {r.value for r in results}
        merges = tuple(
            Merge(value, tuple(sorted(raws)))
            for value, raws in sorted(groups.items())
            if len(raws) > 1 and value in migrated_values
        )
        return MigrationPlan(target, tuple(changes), tuple(results), merges, tuple(unresolved))


def to_nfc(text: str) -> str:
    """Display text: NFC, with bidi controls and logical order kept."""
    return unicodedata.normalize("NFC", text)


def _key_text(text: str) -> str:
    """Matching key: NFC, no invisible format character (bidi), case-folded."""
    kept = "".join(c for c in to_nfc(text) if unicodedata.category(c) != "Cf")
    return " ".join(kept.casefold().split())


def default_registry() -> NormalizerRegistry:
    registry = NormalizerRegistry()
    for name in ("series_name", "team_label", "text_version"):
        registry.register(name, 1, _key_text)
    # Edition key column kept as given but compared in NFC (tombstones, review PR #52).
    registry.register("publisher", 1, to_nfc)
    # `number` and `lang` are owned by their modules; the registry only lists their version.
    registry.register(
        NUMBER_NORMALIZER_NAME, NUMBER_NORMALIZER_VERSION, lambda s: parse_number(s).norm
    )
    # The segment is an output of the number normalizer, re-derived from the raw number.
    registry.register("number_segment", 1, lambda s: parse_number(s).segment)
    registry.register(
        LANG_NORMALIZER_NAME, LANG_NORMALIZER_VERSION, lambda s: normalize_lang(s).tag
    )
    registry.register(
        REMOTE_UNIT_ID_NORMALIZER_NAME,
        REMOTE_UNIT_ID_NORMALIZER_VERSION,
        normalize_remote_unit_id,
    )
    return registry
