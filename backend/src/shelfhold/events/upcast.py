"""Read-time upcast of event payloads (EF-INT-002).

The `event` table is append-only: a row keeps the `schema_version` it was written with. When it is
read back (the SSE replay), its payload is presented at the current version without rewriting the
row. Each step moves one event type from one version to the next; a type with no step at a version
is already in the shape of the next one. A version the reader does not know (zero, or newer than
the code: a database restored from a later release) is refused, never guessed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from shelfhold.events.bus import SCHEMA_VERSION

__all__ = [
    "UPCASTERS",
    "UnreadableVersionError",
    "Upcaster",
    "upcast_payload",
]

Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

# `(event type, from version) -> payload at from version + 1`. Empty while the current version is
# the first one. A new version adds its steps here (and a sample of the old shape to the test); a
# row is never rewritten.
UPCASTERS: dict[tuple[str, int], Upcaster] = {}


class UnreadableVersionError(ValueError):
    """The row carries a version this code cannot present (zero, or from the future)."""


def upcast_payload(
    event_type: str,
    schema_version: int,
    payload: Mapping[str, Any],
    *,
    current: int = SCHEMA_VERSION,
    steps: Mapping[tuple[str, int], Upcaster] | None = None,
) -> dict[str, Any]:
    """`payload` of an `event_type` row written at `schema_version`, at version `current`.

    The input is never modified: a step gets a copy.
    """
    if not 1 <= schema_version <= current:
        raise UnreadableVersionError(f"schema version {schema_version} (current: {current})")
    table = UPCASTERS if steps is None else steps
    result = dict(payload)
    for version in range(schema_version, current):
        step = table.get((event_type, version))
        if step is not None:
            result = step(dict(result))
    return result
