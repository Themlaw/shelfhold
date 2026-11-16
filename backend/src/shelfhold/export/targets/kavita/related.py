"""The "related series" of Kavita, written from the relations of the Works (EF-BIB-039, M4 L19).

A relation links two Works (`catalog.application.relations`); each Work that is a series of a
Kavita library gives that series the relation, on both sides when Kavita has the two words for it
(a sequel is the prequel of what it follows), on the one side when it derives the other itself (an
adaptation or a spin-off: Kavita shows the parent).

What this never does:
- **cross libraries open to different profiles.** A relation is written between two series only if
  their libraries are open to the same profiles: the same ceiling and the same `allowed_profiles`
  on every shelf that holds them. A relation from a child's library to an adults' one would put a
  link to a title she may not see in front of her, in her own Kavita. Nothing is read or written
  for such a pair, and a relation pushed earlier is withdrawn at the next pass when the libraries
  stop being open to the same profiles;
- **write as anyone but the service account.** The call is the target's dedicated administrator
  key, held by the client; no member's key is ever used (the member's accounts are L12's);
- **disturb what a person put there.** Kavita applies each list of an update as a whole (and a list
  left out as empty), so the document read is sent back with only Shelfhold's own relations added
  or removed. What Shelfhold added is kept in the `field_owner` registry, so that only that is
  withdrawn: a link a person had already set is never noted as Shelfhold's;
- **forget what it is about to push.** The registry is written *before* the call (what it held and
  what it is about to add), and cut down to what stands after the answer: a process killed between
  the two leaves a note too long, never too short, and the next pass withdraws what the libraries
  no longer allow;
- **leave a forbidden link in place because a shelf is paused.** Withdrawing is the write that
  protects, so it goes through a pause; adding a link stays suspended until the pause ends.

Nothing is asked of Kavita when no relation has to change: the database alone says so.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    FieldRegistry,
    target_removal_guard,
)
from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog, export

__all__ = ["RELATED_FIELD", "RelatedSeries", "audiences", "kavita_relations"]

log = structlog.get_logger(__name__)
RELATED_FIELD: Final = "related_series"
_NO_CEILING: Final = 99  # a shelf with no ceiling and one that reaches X are open to the same ages

# What a relation (`from` is `kind` of `to`) says in Kavita's words: (series, Kavita kind, the
# series it names), as positions: 0 is `from`, 1 is `to`. Kavita derives the inverse of an
# adaptation or a spin-off (the parent), so those are written once.
_WORDS: Final[dict[str, tuple[tuple[int, str, int], ...]]] = {
    "sequel_of": ((1, "sequels", 0), (0, "prequels", 1)),
    "prequel_of": ((1, "prequels", 0), (0, "sequels", 1)),
    "adaptation_of": ((1, "adaptations", 0),),
    "spin_off_of": ((1, "spinOffs", 0),),
}

Audience = frozenset[tuple[int, frozenset[str]]]


@dataclass(frozen=True)
class _Task:
    kavita_id: int
    destination_id: str
    export_series_id: str
    wanted: dict[str, tuple[int, ...]]  # Kavita kind -> the series ids Shelfhold says it holds
    owned: dict[str, tuple[int, ...]]  # what Shelfhold added, and may therefore take back
    done: dict[str, tuple[int, ...]]  # what `wanted` was when the last push was completed

    @property
    def involved(self) -> set[int]:
        return {self.kavita_id, *(i for ids in (*self.wanted.values(), *self.owned.values())
                                  for i in ids)}  # fmt: skip


def kavita_relations(kind: str, from_series: int, to_series: int) -> list[tuple[int, str, int]]:
    """`(series, Kavita kind, series named)` for one relation; none for a kind Kavita has no
    word for (the vocabulary of Shelfhold is open, the one of Kavita is not)."""
    pair = (from_series, to_series)
    return [(pair[a], word, pair[b]) for a, word, b in _WORDS.get(kind, ())]


def audiences(connection: Connection, target_id: str) -> dict[str, Audience]:
    """For each remote library of the target: who it is open to, as the set of
    `(ceiling, profiles)` of the shelves linked to it (`profiles` empty: everyone). Two libraries
    are open to the same profiles when their audiences are equal."""
    shelf, destination = export.shelf.c, export.shelf_destination.c
    link = export.shelf_destination_remote_library.c
    found: dict[str, set[tuple[int, frozenset[str]]]] = defaultdict(set)
    for row in connection.execute(
        sa.select(link.remote_library_id, shelf.max_age_rating, shelf.allowed_profiles)
        .select_from(
            export.shelf_destination_remote_library.join(
                export.shelf_destination, destination.id == link.shelf_destination_id
            ).join(export.shelf, shelf.id == destination.shelf_id)
        )
        .where(link.export_target_id == target_id)
    ):
        profiles = row.allowed_profiles if isinstance(row.allowed_profiles, list) else []
        ceiling = _NO_CEILING if row.max_age_rating is None else int(row.max_age_rating)
        found[str(row.remote_library_id)].add((ceiling, frozenset(str(p) for p in profiles)))
    return {library: frozenset(audience) for library, audience in found.items()}


class RelatedSeries:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    def due(self, target_id: str) -> list[_Task]:
        """What has to change in Kavita, from the database alone."""
        with self._engine.connect() as connection:
            return self._tasks(connection, target_id)

    async def publish(self, client: KavitaClient, target_id: str) -> list[int]:
        """Write the relations that changed, as the service account; the Kavita series ids that
        were written. Nothing is sent when the database says nothing changed. A withdrawal goes
        through a paused shelf, an addition does not."""
        tasks = self.due(target_id)
        if not tasks:
            return []
        try:
            known = await client.series_by_ids(sorted({i for t in tasks for i in t.involved}))
        except TargetError as error:
            log.info("export.related_failed", target_id=target_id, reason=type(error).__name__)
            return []
        removal_guard = target_removal_guard(self._engine, target_id)
        written: list[int] = []
        for task in tasks:
            if task.kavita_id not in known:
                continue  # Kavita has not indexed it (or no longer has it): the cache is stale
            wanted = {k: tuple(i for i in ids if i in known) for k, ids in task.wanted.items()}
            try:
                changed = await self._write(client, task, wanted, removal_guard)
            except TargetError as error:  # Kavita unreachable: the next pass writes it again
                log.info("export.related_failed", target_id=target_id,
                         reason=type(error).__name__)  # fmt: skip
                break
            if changed:
                written.append(task.kavita_id)
        return written

    async def _write(
        self,
        client: KavitaClient,
        task: _Task,
        wanted: Mapping[str, tuple[int, ...]],
        removal_guard: Callable[[], None],
    ) -> bool:
        """Read what Kavita holds, take out what Shelfhold added, put what it wants, send it back
        whole. False when it already holds exactly that (or nothing could be sent)."""
        current = await client.series_related(task.kavita_id)
        held = {kind: sorted(current.get(kind, ())) for kind in dto.RELATION_KINDS}
        # what a person put there stays theirs, even when Shelfhold says the same thing
        foreign = {k: set(held[k]) - set(task.owned.get(k, ())) for k in dto.RELATION_KINDS}
        mine = {k: tuple(sorted(set(wanted.get(k, ())) - foreign[k])) for k in dto.RELATION_KINDS}
        full = {k: sorted(foreign[k] | set(mine[k])) for k in dto.RELATION_KINDS}
        if full == held:
            self._record(task, mine, wanted)
            return False
        adds = any(set(full[k]) - set(held[k]) for k in dto.RELATION_KINDS)
        # the note first: a death between the call and the end of this pass leaves it too long
        self._note(task, _union(task.owned, mine), task.done)
        try:
            if adds:
                await client.set_series_related(task.kavita_id, full)
            else:
                with client.guarded_as(removal_guard):
                    await client.set_series_related(task.kavita_id, full)
        except DestinationPausedError:
            # a paused destination takes no addition: take back what is not allowed, and ask again
            only = {k: sorted(set(held[k]) - (set(task.owned.get(k, ())) - set(mine[k])))
                    for k in dto.RELATION_KINDS}  # fmt: skip
            if only == held:
                self._note(task, task.owned, task.done)
                return False
            with client.guarded_as(removal_guard):
                await client.set_series_related(task.kavita_id, only)
            kept = {k: tuple(sorted(set(task.owned.get(k, ())) & set(mine[k])))
                    for k in dto.RELATION_KINDS}  # fmt: skip
            self._note(task, kept, task.done)
            return True
        self._record(task, mine, wanted)
        return True

    def _note(
        self,
        task: _Task,
        owned: Mapping[str, tuple[int, ...]],
        done: Mapping[str, tuple[int, ...]],
    ) -> None:
        """What the registry says of the series: `owned` (may be taken back), and `done` (the
        `wanted` of the last completed push, so that anything else is due again)."""
        value = {"owned": _listed(owned), "done": _listed(done)}
        with write_transaction(self._engine) as connection:
            FieldRegistry(connection).record_push(
                task.destination_id, task.export_series_id, RELATED_FIELD, value,
                self._clock.now(),
            )  # fmt: skip

    def _record(
        self,
        task: _Task,
        owned: Mapping[str, tuple[int, ...]],
        wanted: Mapping[str, tuple[int, ...]],
    ) -> None:
        """After Kavita's answer: only what Shelfhold added stays, and the push is complete."""
        self._note(task, owned, wanted)

    # -- what is due -----------------------------------------------------------------------

    def _tasks(self, connection: Connection, target_id: str) -> list[_Task]:
        by_library = audiences(connection, target_id)
        series_of = self._series_of_works(connection, target_id)
        wanted: dict[int, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
        table = catalog.work_relation
        for row in connection.execute(
            sa.select(table.c.from_work_id, table.c.to_work_id, table.c.kind)
        ):
            for left in series_of.get(row.from_work_id, ()):
                for right in series_of.get(row.to_work_id, ()):
                    if left.kavita_id == right.kavita_id:
                        continue
                    audience = by_library.get(left.library_id)
                    if audience is None or audience != by_library.get(right.library_id):
                        continue  # libraries open to different profiles: never
                    for own, word, other in kavita_relations(
                        row.kind, left.kavita_id, right.kavita_id
                    ):
                        wanted[own][word].add(other)
        owners = self._owners(connection, target_id)
        registry = FieldRegistry(connection)
        tasks: list[_Task] = []
        for kavita_id in sorted(set(wanted) | set(owners)):
            owner = owners.get(kavita_id)
            if owner is None:
                continue  # no series row to keep the registry on: nothing to write about
            kept = registry.get(owner[0], owner[1], RELATED_FIELD)
            owned, done = _noted(kept.last_value) if kept is not None and kept.pushed else ({}, {})
            want = {k: tuple(sorted(v)) for k, v in wanted.get(kavita_id, {}).items() if v}
            leftover = any(set(ids) - set(want.get(kind, ())) for kind, ids in owned.items())
            if want != done or leftover:
                tasks.append(_Task(kavita_id, owner[0], owner[1], want, owned, done))
        return tasks

    @staticmethod
    def _series_of_works(connection: Connection, target_id: str) -> dict[str, list[_Series]]:
        """The Kavita series each Work of a relation is, in the libraries of this target."""
        edition, link = catalog.edition.c, export.edition_export_series.c
        series, library = export.export_series.c, export.remote_library.c
        relation = catalog.work_relation
        in_relation = sa.union(
            sa.select(relation.c.from_work_id.label("id")),
            sa.select(relation.c.to_work_id.label("id")),
        ).subquery()
        found: dict[str, list[_Series]] = defaultdict(list)
        for row in connection.execute(
            sa.select(edition.work_id, series.kavita_series_id, link.remote_library_id)
            .select_from(
                catalog.edition.join(export.edition_export_series, link.edition_id == edition.id)
                .join(export.export_series, series.id == link.export_series_id)
                .join(export.remote_library, library.id == link.remote_library_id)
            )
            .where(
                library.export_target_id == target_id,
                edition.work_id.in_(sa.select(in_relation.c.id)),
                series.kavita_series_id.is_not(None),
            )
            .order_by(edition.work_id, series.kavita_series_id)
        ):
            if str(row.kavita_series_id).isdigit():
                found[str(row.work_id)].append(
                    _Series(int(row.kavita_series_id), str(row.remote_library_id))
                )
        return found

    @staticmethod
    def _owners(connection: Connection, target_id: str) -> dict[int, tuple[str, str]]:
        """`kavita series id -> (destination, export series)` for every indexed series of the
        target: where the registry keeps what Shelfhold pushed on it."""
        series, library = export.export_series.c, export.remote_library.c
        found: dict[int, tuple[str, str]] = {}
        for row in connection.execute(
            sa.select(series.id, series.shelf_destination_id, series.kavita_series_id)
            .select_from(
                export.export_series.join(
                    export.remote_library, library.id == series.remote_library_id
                )
            )
            .where(library.export_target_id == target_id, series.kavita_series_id.is_not(None))
            .order_by(series.id)
        ):
            if str(row.kavita_series_id).isdigit():
                found.setdefault(
                    int(row.kavita_series_id), (str(row.shelf_destination_id), str(row.id))
                )
        return found


@dataclass(frozen=True)
class _Series:
    kavita_id: int
    library_id: str


def _normal(value: Any) -> dict[str, tuple[int, ...]]:
    """One list of the registry's note as it is compared: kind -> sorted ids (anything else is
    nothing)."""
    if not isinstance(value, dict):
        return {}
    return {
        str(k): tuple(sorted(int(i) for i in v if isinstance(i, int)))
        for k, v in value.items()
        if isinstance(v, list) and v
    }


def _noted(value: Any) -> tuple[dict[str, tuple[int, ...]], dict[str, tuple[int, ...]]]:
    """`(owned, done)` of the registry's note."""
    if not isinstance(value, dict):
        return {}, {}
    return _normal(value.get("owned")), _normal(value.get("done"))


def _listed(value: Mapping[str, tuple[int, ...]]) -> dict[str, list[int]]:
    return {kind: list(ids) for kind, ids in sorted(value.items()) if ids}


def _union(
    left: Mapping[str, tuple[int, ...]], right: Mapping[str, tuple[int, ...]]
) -> dict[str, tuple[int, ...]]:
    return {
        kind: tuple(sorted(set(left.get(kind, ())) | set(right.get(kind, ()))))
        for kind in {*left, *right}
    }
