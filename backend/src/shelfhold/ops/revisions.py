"""`revisions list|apply` (EF-BIB-043, EF-BIB-024): the revisions recorded without replacing a file.

`list` queues what was recorded before the queue existed, then shows the queue with the KOReader
impact and the batch token. `apply` first pushes the metadata-only revisions through the Kavita
API (no file touched), then, with the token typed back, asks for the rewrites. Both doors that
change served files are reserved actions: `revisions.apply`, and `revisions.force` for a revision
the guard quarantined. A fresh session or the host CLI, never a key; audited.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.catalog.public import (
    ConfirmationError,
    ForceRefusedError,
    PendingRevision,
    RevisionQueue,
    RewriteUnavailableError,
)
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.series import SeriesFields
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.ops.events import BusTargetEvents

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["RevisionOps"]


def _view(item: PendingRevision) -> dict[str, Any]:
    return {
        "id": item.id, "unit_id": item.unit_id, "variant_id": item.variant_id,
        "revision": item.revision, "nature": item.nature, "suspect": item.suspect,
        "field": item.field, "read": item.read, "koreader_reset": item.koreader_reset,
    }  # fmt: skip


class _KavitaFields:
    """`MetadataPusher` over the Kavita series of the Unit's Edition. It opens its own
    connections: the network is never awaited inside a write transaction."""

    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def push(self, connection: sa.Connection, *, unit_id: str, field: str, value: str) -> bool:
        del connection
        return self.push_unit(unit_id, field, value)

    def push_unit(self, unit_id: str, field: str, value: str) -> bool:
        targets = self._series_of(unit_id)
        if not targets:
            return False
        pusher = SeriesFields(
            self._comp.engine, self._comp.clock, BusTargetEvents(self._comp.engine, self._comp.bus)
        )
        taken = True
        for destination_id, target_id, export_series_id, kavita_id in targets:
            client = self._comp.kavita_client(target_id)
            try:
                taken &= asyncio.run(
                    pusher.push(client, destination_id, export_series_id, kavita_id, field, value)
                )
            except TargetError:
                return False  # Kavita does not answer: the revision goes to the rewrite queue
        return taken

    def _series_of(self, unit_id: str) -> list[tuple[str, str, str, int]]:
        u = sa.table("unit", sa.column("id"), sa.column("edition_id"))
        series, link, dest = (
            export.export_series,
            export.edition_export_series,
            export.shelf_destination,
        )
        with self._comp.engine.connect() as connection:
            rows = connection.execute(
                sa.select(series.c.shelf_destination_id, dest.c.target_id, series.c.id,
                          series.c.kavita_series_id)
                .select_from(
                    u.join(link, link.c.edition_id == u.c.edition_id)
                    .join(series, series.c.id == link.c.export_series_id)
                    .join(dest, dest.c.id == series.c.shelf_destination_id)
                )
                .where(u.c.id == unit_id, series.c.kavita_series_id.is_not(None))
            ).all()  # fmt: skip
        return [(r[0], r[1], r[2], int(r[3])) for r in rows]


class RevisionOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def list_pending(self) -> dict[str, Any]:
        with self._comp.engine.begin() as connection:
            queue = RevisionQueue(connection)
            queued = queue.enqueue_missing()
            report = queue.report()
        return {
            "items": [_view(i) for i in report.items],
            "koreader_positions": report.koreader_positions,
            "token": report.token,
            "newly_queued": queued,
        }

    def apply(
        self,
        *,
        confirm: str | None,
        include_read: bool,
        force_ids: tuple[str, ...] = (),
        principal: Principal,
    ) -> dict[str, Any]:
        action = RESERVED_ACTIONS["revisions.force" if force_ids else "revisions.apply"]
        verdict = authorize_reserved(
            principal, action, now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the route and the socket decided first
            raise CodedRefusal("forbidden", type(verdict).__name__)
        with self._comp.engine.begin() as connection:
            RevisionQueue(connection).enqueue_missing()
        pushed = self._push_metadata()
        try:
            with self._comp.engine.begin() as connection:
                done = RevisionQueue(connection, requester=None).apply_rewrites(
                    confirm, include_read=include_read, force=force_ids,
                    grant=verdict if force_ids else None,
                )  # fmt: skip
                self._audit(connection, principal, action.name, len(done.requested))
        except ConfirmationError as error:
            raise CodedRefusal("revisions.confirmation_required", str(error)) from None
        except ForceRefusedError:
            raise CodedRefusal("forbidden", "revisions.force") from None
        except RewriteUnavailableError:
            raise ExitUnavailableError("reacquisition is not connected") from None
        return {
            "metadata_applied": pushed,
            "requested": len(done.requested),
            "skipped_read": len(done.skipped_read),
            "skipped_suspect": len(done.skipped_suspect),
        }

    def _push_metadata(self) -> int:
        """One transaction per row: a crash between the push and the mark replays the same value."""
        with self._comp.engine.connect() as connection:
            candidates = RevisionQueue(connection).metadata_candidates()
        pusher = _KavitaFields(self._comp)
        pushed = 0
        for item in candidates:
            if item.field is None or item.value is None:
                continue
            taken = pusher.push_unit(item.unit_id, item.field, item.value)
            with self._comp.engine.begin() as connection:
                RevisionQueue(connection).settle_metadata(item.id, taken=taken)
            pushed += int(taken)
        return pushed

    @staticmethod
    def _audit(connection: sa.Connection, principal: Principal, action: str, count: int) -> None:
        connection.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=principal.actor_id, action=action,
                target=f"revisions:{count}", session_id=principal.session_id,
                auth_time=principal.auth_time,
            )
        )  # fmt: skip
