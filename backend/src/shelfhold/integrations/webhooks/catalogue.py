"""What a webhook may carry: a closed list of events, each with the versions of its shape.

An event type is exposed only if it is named here, with the fields of its payload that leave the
house (ids, counts and codes: never a title, a path, an address or a person). Each type has a
current version of its shape. When the shape changes, the old one is **deprecated** for a bounded
window: during it the outbox makes both (a receiver can move over without losing an event), and
after the sunset only the new one. The bounds are checked when the catalogue is built: at most one
old version per type, and a window of at most `MAX_WINDOW`. A type, a version or a field that is
not listed is never sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

__all__ = [
    "MAX_WINDOW",
    "WEBHOOK_EVENTS",
    "Catalogue",
    "CatalogueError",
    "Deprecation",
    "EventSpec",
    "clean_payload",
    "pick",
]

MAX_WINDOW: Final = timedelta(days=180)
_TOKEN: Final = re.compile(r"[A-Za-z0-9_:-]{1,64}")  # no slash, no dot, no space: no path, no host
_PERSON: Final = re.compile(r"(?:user|actor|requester|owner|subject|profile)_id\Z|by\Z")

Shape = Callable[[Mapping[str, Any]], dict[str, Any]]


class CatalogueError(ValueError):
    """A catalogue that breaks its own bounds: it is refused when it is built, not at a delivery."""


def clean_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """What of a payload may leave: a boolean, a number, or a token (no slash, no dot, no space);
    never a key that names a person. Whatever else an event happened to carry is dropped here,
    not trusted to be absent."""
    kept: dict[str, Any] = {}
    for key, value in payload.items():
        if _PERSON.fullmatch(key):
            continue
        if isinstance(value, bool | int) or (isinstance(value, str) and _TOKEN.fullmatch(value)):
            kept[key] = value
    return kept


class _Pick:
    """A shape that keeps these fields, when the payload has them; it can name them (the
    description of the API lists them)."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        self.fields = keys

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {key: payload[key] for key in self.fields if key in payload}


def pick(*keys: str) -> Shape:
    return _Pick(keys)


@dataclass(frozen=True)
class Deprecation:
    """An old version still made beside the current one, from `since` (included) to `sunset`."""

    since: datetime
    sunset: datetime


@dataclass(frozen=True)
class EventSpec:
    type: str
    current: int
    render: Mapping[int, Shape]  # version -> the shape of the payload at that version
    deprecated: Mapping[int, Deprecation] = field(default_factory=dict)


class Catalogue:
    def __init__(self, specs: Iterable[EventSpec]) -> None:
        self._specs: dict[str, EventSpec] = {}
        for spec in specs:
            self._check(spec)
            if spec.type in self._specs:
                raise CatalogueError(f"{spec.type} is listed twice")
            self._specs[spec.type] = spec

    @staticmethod
    def _check(spec: EventSpec) -> None:
        if spec.current < 1 or spec.current not in spec.render:
            raise CatalogueError(f"{spec.type}: the current version has no shape")
        if len(spec.deprecated) > 1:
            raise CatalogueError(f"{spec.type}: one old version at a time")
        for version, window in spec.deprecated.items():
            if not 1 <= version < spec.current or version not in spec.render:
                raise CatalogueError(f"{spec.type}: version {version} is not an older shape")
            if window.sunset <= window.since:
                raise CatalogueError(f"{spec.type}: the sunset is not after the start")
            if window.sunset - window.since > MAX_WINDOW:
                raise CatalogueError(f"{spec.type}: a deprecation lasts {MAX_WINDOW.days} days")

    def specs(self) -> list[EventSpec]:
        return list(self._specs.values())

    def types(self) -> frozenset[str]:
        return frozenset(self._specs)

    def versions_for(self, event_type: str, now: datetime) -> list[int]:
        """The versions to make for an event now: the old one while its window is open, then the
        current one. Empty for a type that is not exposed."""
        spec = self._specs.get(event_type)
        if spec is None:
            return []
        old = [v for v, w in spec.deprecated.items() if w.since <= now < w.sunset]
        return [*old, spec.current]

    def render(
        self, event_type: str, version: int, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The payload at `version`, cleaned; None when the type or the version is not exposed."""
        spec = self._specs.get(event_type)
        shape = None if spec is None else spec.render.get(version)
        return None if shape is None else clean_payload(shape(payload))


_FIELDS = (
    "count", "state", "reason", "code", "kind", "number", "work_id", "series_id", "edition_id",
    "unit_id",
)  # fmt: skip
_EXPOSED = (
    "unit.stored", "unit.upgraded", "unit.revision_suspect", "series.gap", "series.stale",
    "series.removed_upstream", "source.broken", "source.fallback", "source.auth_expired",
    "disk.low", "backup.failed", "backup.stale", "reconcile.drift", "job.dead_lettered",
    "target.unreachable", "target.forbidden", "metadata.provider_down",
)  # fmt: skip

WEBHOOK_EVENTS: Final = Catalogue(
    [
        *(EventSpec(type=name, current=1, render={1: pick(*_FIELDS)}) for name in _EXPOSED),
        # M6 L23a: a delivery that was given up. A new type, not a new shape: the first version,
        # no deprecation window (nothing that a receiver already parses changes).
        EventSpec(
            type="webhook.dead",
            current=1,
            render={1: pick("subscription_id", "event_id", "reason")},
        ),
    ]
)
