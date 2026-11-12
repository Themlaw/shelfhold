"""`project_event`: the projection of an event for a reader, with the visibility filter (L09).

`activity_view` (EF-USR-016) decides on the role, the owner and the `visibility` of the event. A
reader with a ceiling (a child) also never sees an event about something she may not see: an event
that names a Work, an Edition, a series or a Unit (any key of the payload ending in `work_id`,
`edition_id`, `series_id`, `unit_id`, or their plural lists) is shown only if **every** thing it
names is visible to her under the one filter of the catalogue. A name that is not found is hidden
too (absent and hidden are one answer), and so is an event that names a Work beside a hidden one.

The activity page, a notification and an SSE frame all call this function: an event reaches a
reader through no other door.
"""

from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Connection

from shelfhold.catalog.public import (
    ReaderScope,
    visible_edition_ids,
    visible_series_ids,
    visible_unit_ids,
    visible_work_ids,
)
from shelfhold.events.activity import ActivityEvent, ActivityItem, Viewer, activity_view

__all__ = ["EventRefs", "event_refs", "project_event"]

_KINDS = ("work", "edition", "series", "unit")
_DEPTH = 4


@dataclass(frozen=True)
class EventRefs:
    works: frozenset[str] = field(default_factory=frozenset)
    editions: frozenset[str] = field(default_factory=frozenset)
    series: frozenset[str] = field(default_factory=frozenset)
    units: frozenset[str] = field(default_factory=frozenset)
    opaque: bool = False  # nested past what is read: what it names cannot be known (fail closed)

    def __bool__(self) -> bool:
        return bool(self.works or self.editions or self.series or self.units or self.opaque)


def _walk(value: object, depth: int) -> Iterator[tuple[str, object]]:
    """Every `(key, value)` of the payload, with a marker `("", None)` where it is too deep."""
    if isinstance(value, Mapping):
        if depth >= _DEPTH:
            yield "", None
            return
        for key, item in value.items():
            if isinstance(key, str):
                yield key, item
            yield from _walk(item, depth + 1)
    elif isinstance(value, list | tuple):
        if depth >= _DEPTH:
            yield "", None
            return
        for item in value:
            yield from _walk(item, depth + 1)


def event_refs(payload: Mapping[str, object]) -> EventRefs:
    """What an event payload names: every key ending in `<kind>_id` (a string) or `<kind>_ids`
    (a list of strings). A payload nested deeper than `_DEPTH` is `opaque`: a reader with a
    ceiling is never shown what cannot be read."""
    found: dict[str, set[str]] = {kind: set() for kind in _KINDS}
    opaque = False
    for key, value in _walk(payload, 0):
        if key == "" and value is None:
            opaque = True
            continue
        for kind in _KINDS:
            if key.endswith(f"{kind}_id"):
                if isinstance(value, str):
                    found[kind].add(value)
            elif key.endswith(f"{kind}_ids") and isinstance(value, list | tuple):
                found[kind].update(v for v in value if isinstance(v, str))
    return EventRefs(
        frozenset(found["work"]), frozenset(found["edition"]), frozenset(found["series"]),
        frozenset(found["unit"]), opaque,
    )  # fmt: skip


def _all_visible(connection: Connection, scope: ReaderScope, refs: EventRefs) -> bool:
    if refs.opaque:
        return False
    for named, visible in (
        (refs.works, visible_work_ids),
        (refs.editions, visible_edition_ids),
        (refs.series, visible_series_ids),
        (refs.units, visible_unit_ids),
    ):
        if named and visible(connection, scope, named) != set(named):
            return False
    return True


def project_event(
    connection: Connection,
    viewer: Viewer,
    scope: ReaderScope,
    event: ActivityEvent,
    *,
    editions: Collection[str] = (),
) -> ActivityItem | None:
    """What `viewer` may see of `event`, or None. `editions`: Editions the event is about that
    its payload does not name (a download request names its Variant, not its Edition)."""
    item = activity_view(viewer, event)
    if item is None or scope.unrestricted:
        return item
    refs = event_refs(event.payload)
    extra = frozenset(editions)
    if extra:
        refs = EventRefs(refs.works, refs.editions | extra, refs.series, refs.units, refs.opaque)
    if scope.kind != "ceiling":
        return None  # closed by default: a scope that is neither an adult's nor a child's
    return item if _all_visible(connection, scope, refs) else None
