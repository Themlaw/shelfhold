"""Plugin registry: validates manifests, negotiates ``api``, loads first-party plugins only.

M1a loads in-process first-party plugins and nothing else (ENF-SEC-009): third-party plugins are
disabled and their code is never imported, since origin is decided from the distribution that
ships the entry point, before anything is loaded. The guard on installation and the isolation of
third-party plugins come in M1b and M6.

Compatibility (EF-SRC-001): the host accepts ``api == N`` natively and ``api == N-1`` through a shim
that rewrites the old manifest into the current shape; any other value is refused with a reason and
the remaining plugins still load.
"""

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from typing import Any

from shelfhold.sdk.models import API_VERSION, PluginKind, PluginManifest
from shelfhold.sdk.source import SourceManifest

ENTRY_POINT_GROUP = "shelfhold.plugins"
FIRST_PARTY_DISTRIBUTIONS = frozenset({"shelfhold"})

_LOADED = frozenset({"loaded", "loaded_shim"})
ManifestShim = Callable[[dict[str, Any]], dict[str, Any]]


class PluginOrigin(StrEnum):
    FIRST_PARTY = "first_party"
    THIRD_PARTY = "third_party"


class PluginStatus(StrEnum):
    LOADED = "loaded"
    LOADED_SHIM = "loaded_shim"  # api N-1, translated by the shim
    DISABLED = "disabled"  # third party in M1a
    INCOMPATIBLE = "incompatible"  # api out of range, invalid manifest, load failure


@dataclass(frozen=True)
class ApiVerdict:
    status: PluginStatus
    reason: str | None = None


def negotiate_api(declared: int, current: int = API_VERSION) -> ApiVerdict:
    """N loads, N-1 loads through the shim, everything else is incompatible."""
    if declared == current:
        return ApiVerdict(PluginStatus.LOADED)
    if declared == current - 1:
        return ApiVerdict(PluginStatus.LOADED_SHIM)
    return ApiVerdict(
        PluginStatus.INCOMPATIBLE,
        f"api {declared} is incompatible: this host accepts {current} and {current - 1}",
    )


@dataclass(frozen=True)
class PluginCandidate:
    """A plugin found by discovery. `load` is called only for first-party plugins and returns an
    object whose ``manifest`` attribute is a manifest model or its raw dict."""

    key: str
    origin: PluginOrigin
    load: Callable[[], Any]


@dataclass(frozen=True)
class PluginRecord:
    key: str
    origin: PluginOrigin
    status: PluginStatus
    reason: str | None = None
    manifest: PluginManifest | None = None
    instance: Any = None


def _normalise(name: str) -> str:
    """PEP 503 normalisation of a distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def candidates_from_entry_points(entry_points: Iterable[Any]) -> list[PluginCandidate]:
    """Classify entry points by the distribution that ships them; nothing is imported here."""
    first_party = {_normalise(n) for n in FIRST_PARTY_DISTRIBUTIONS}
    found = []
    for ep in entry_points:
        dist = getattr(ep, "dist", None)
        dist_name = getattr(dist, "name", None)
        trusted = isinstance(dist_name, str) and _normalise(dist_name) in first_party
        origin = PluginOrigin.FIRST_PARTY if trusted else PluginOrigin.THIRD_PARTY
        found.append(PluginCandidate(key=ep.name, origin=origin, load=ep.load))
    return found


def discover() -> list[PluginCandidate]:
    """Candidates registered under the ``shelfhold.plugins`` entry-point group."""
    return candidates_from_entry_points(metadata.entry_points(group=ENTRY_POINT_GROUP))


class PluginRegistry:
    def __init__(
        self,
        *,
        api_version: int = API_VERSION,
        shims: Mapping[int, ManifestShim] | None = None,
        active_keys: Collection[str] | None = None,
    ) -> None:
        self._api = api_version
        self._shims = dict(shims or {})
        self._active_keys = None if active_keys is None else frozenset(active_keys)
        self._records: list[PluginRecord] = []

    @property
    def records(self) -> list[PluginRecord]:
        return list(self._records)

    def get(self, key: str) -> PluginRecord | None:
        """The record that holds the key: the loaded one if any, else the first seen."""
        matching = [r for r in self._records if r.key == key]
        loaded = [r for r in matching if r.status in _LOADED]
        return (loaded or matching)[0] if matching else None

    def active(self) -> list[PluginRecord]:
        """Loaded plugins that are switched on; a fresh install switches none on (ENF-LEG-001)."""
        return [
            r
            for r in self._records
            if r.status in _LOADED and (self._active_keys is None or r.key in self._active_keys)
        ]

    def load_all(self, candidates: Iterable[PluginCandidate]) -> list[PluginRecord]:
        """Load every candidate; one failing plugin never prevents the others from loading.

        Candidates are grouped by key before any ``load()``: a key is decided on origins alone, so
        a homonym can neither shadow a first-party plugin nor make two plugins load. One first-party
        candidate wins and its third-party homonyms are refused unloaded; several first-party
        candidates with one key are all refused.
        """
        items = list(candidates)
        groups: dict[str, list[PluginCandidate]] = {}
        for candidate in items:
            groups.setdefault(candidate.key, []).append(candidate)
        decided: dict[int, PluginRecord] = {}
        for key, group in groups.items():
            firsts = [c for c in group if c.origin is PluginOrigin.FIRST_PARTY]
            taken = self.get(key)
            held = taken is not None and taken.status in _LOADED
            for c in group:
                if len(firsts) > 1 and c.origin is PluginOrigin.FIRST_PARTY:
                    why = f"duplicate_key: {len(firsts)} first-party plugins declare {key!r}"
                elif held or (firsts and c.origin is not PluginOrigin.FIRST_PARTY):
                    why = f"duplicate_key: {key!r} is already held by a first-party plugin"
                else:
                    continue
                decided[id(c)] = PluginRecord(key, c.origin, PluginStatus.INCOMPATIBLE, why)
        records = [decided.get(id(c)) or self._load(c) for c in items]
        self._records.extend(records)
        return records

    def _load(self, candidate: PluginCandidate) -> PluginRecord:
        key, origin = candidate.key, candidate.origin
        if origin is not PluginOrigin.FIRST_PARTY:
            return PluginRecord(
                key, origin, PluginStatus.DISABLED, "third-party plugins are disabled in M1a"
            )
        try:
            instance = candidate.load()
            manifest, status = self._manifest(getattr(instance, "manifest", None))
        except Exception as exc:  # a broken plugin is reported, never raised
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, f"load failed: {exc}")
        if manifest is None:
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, status.reason)
        if manifest.key != key:
            reason = f"manifest key {manifest.key!r} differs from entry point {key!r}"
            return PluginRecord(key, origin, PluginStatus.INCOMPATIBLE, reason)
        return PluginRecord(key, origin, status.status, None, manifest, instance)

    def _manifest(self, raw: Any) -> tuple[PluginManifest | None, ApiVerdict]:
        data = raw.model_dump(mode="json") if isinstance(raw, PluginManifest) else raw
        if not isinstance(data, Mapping):
            return None, ApiVerdict(PluginStatus.INCOMPATIBLE, "plugin exposes no manifest")
        declared = data.get("api")
        if not isinstance(declared, int) or isinstance(declared, bool):
            return None, ApiVerdict(PluginStatus.INCOMPATIBLE, "manifest declares no integer api")
        verdict = negotiate_api(declared, self._api)
        if verdict.status is PluginStatus.INCOMPATIBLE:
            return None, verdict
        migrated = dict(data)
        if verdict.status is PluginStatus.LOADED_SHIM:
            shim = self._shims.get(declared)
            migrated = shim(migrated) if shim else migrated
            migrated["api"] = self._api
        model: type[PluginManifest] = (
            SourceManifest if migrated.get("kind") == PluginKind.SOURCE else PluginManifest
        )
        return model.model_validate(migrated), verdict
