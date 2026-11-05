"""A reinstalled Kavita, found again by path (EF-EXP-039).

The relative path of the manifest is the key; the ids of Kavita (library, series, chapter) are a
cache, read again. The fingerprint of the instance (URL and installation id) is compared at every
connection: if it changes, `target.instance_changed` is emitted and the exports wait. `reconnect`
(CLI `shelfhold target reconnect`) then re-resolves each library by its folder, creates none that
exists, applies the settings again, forgets the old ids and invalidates the `field_owner` registry
(the rated ages are pushed again by M1b). No file is written and no manifest line is rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.target_state import (
    forget_remote_cache,
    invalidate_field_owners,
    load_fingerprint,
    save_fingerprint,
    save_health,
)
from shelfhold.export.domain.kavita_instance import (
    instance_changed,
    instance_fingerprint,
    match_library,
)
from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import ProtocolError
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import export

__all__ = ["InstanceService", "ReconnectReport", "library_type_of"]

_TEMP: Final = "reinstall:"  # while the ids of two libraries are swapped (unique per target)


def library_type_of(raw: str | None) -> dto.LibraryType:
    """`shelf_destination.kavita_library_type` ("manga", "light_novel", "Manga"...)."""
    wanted = (raw or "manga").replace("_", "").replace("-", "").lower()
    for member in dto.LibraryType:
        if member.name.replace("_", "").lower() == wanted:
            return member
    raise ValueError(f"unknown Kavita library type {raw!r}")


@dataclass
class ReconnectReport:
    re_resolved: dict[str, tuple[str, str]] = field(default_factory=dict)  # library -> (old, new)
    created: list[str] = field(default_factory=list)  # remote_library ids that had no library
    settings_applied: list[str] = field(default_factory=list)
    skipped_adopted: list[str] = field(default_factory=list)  # never touched unconfirmed
    invalidated_fields: int = 0
    audit_failed: bool = False
    paused_shelves: list[str] = field(default_factory=list)  # held by the audit that follows


class InstanceService:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    async def check(self, client: KavitaClient, target_id: str) -> bool:
        """At every connection: False (and exports paused) when it is not the same Kavita."""
        await client.probe()
        health = client.health
        current = await self._fingerprint(client)
        with self._engine.begin() as connection:
            stored = load_fingerprint(connection, target_id)
            if stored is None:
                if current is not None:
                    save_fingerprint(
                        connection, target_id, current, health.version, self._clock.now()
                    )
            elif instance_changed(stored, current) and not health.instance_changed:
                health.instance_changed = True
                health.refresh_waiting()
                self._events.emit("target.instance_changed", target_id=target_id)
            save_health(connection, target_id, health)
        return not health.instance_changed

    @staticmethod
    async def _fingerprint(client: KavitaClient) -> str | None:
        """URL + installation id + creation date of the oldest account (see the domain rule)."""
        created: str | None = None
        try:
            dates = [m.created for m in await client.list_users() if m.created]
            created = min(dates) if dates else None
        except Exception:  # unreadable accounts: the fingerprint cannot match a stored one
            created = "unreadable"
        return instance_fingerprint(client.base_url, client.health.install_id, created)

    async def reconnect(
        self, client: KavitaClient, target_id: str, *, auditor: AccessAuditor
    ) -> ReconnectReport:
        """`target reconnect`: the same folders, a new server. Nothing is rewritten.

        The access audit runs before the pause of the changed instance is lifted: if it fails,
        or finds an exposed library, the shelves stay paused.
        """
        with client.unguarded():  # the library settings are written while the exports wait
            return await self._reconnect(client, target_id, auditor)

    async def _reconnect(
        self, client: KavitaClient, target_id: str, auditor: AccessAuditor
    ) -> ReconnectReport:
        assessment = await client.probe()
        current = await self._fingerprint(client)
        if current is None:
            raise ProtocolError("the server gives no installation id")
        libraries = await client.list_libraries()
        report = ReconnectReport()
        with self._engine.begin() as connection:
            wanted = self._wanted(connection, target_id)
        resolved: dict[str, str] = {}
        for item in wanted:
            found = match_library(libraries, item.remote_root)
            if found is None:
                if assessment.mode.value == "degraded_readonly":
                    continue
                created = await client.create_library(
                    item.shelf_name, library_type_of(item.library_type), [item.remote_root]
                )
                resolved[item.remote_library_id] = str(created["id"])
                report.created.append(item.remote_library_id)
                continue
            resolved[item.remote_library_id] = str(found["id"])
            if item.adopted:
                report.skipped_adopted.append(item.remote_library_id)
            elif assessment.mode.value != "degraded_readonly":
                await client.ensure_exclude_patterns(int(str(found["id"])), dto.HIDDEN_DIR_PATTERNS)
                report.settings_applied.append(item.remote_library_id)
        with self._engine.begin() as connection:
            report.re_resolved = self._store_refs(connection, target_id, resolved)
            forget_remote_cache(connection, target_id)
            report.invalidated_fields = invalidate_field_owners(connection, target_id)
            save_fingerprint(
                connection, target_id, current, client.health.version, self._clock.now()
            )
        audit = await auditor.audit(client, target_id)  # fails closed: the pause then stays
        report.audit_failed = audit.failed
        report.paused_shelves = list(audit.paused_shelves)
        with self._engine.begin() as connection:
            client.health.instance_changed = False
            client.health.refresh_waiting()
            save_health(connection, target_id, client.health)
        return report

    # -- the database -------------------------------------------------------------------------

    def _wanted(self, connection: sa.Connection, target_id: str) -> list[_Wanted]:
        d, s, link = export.shelf_destination, export.shelf, export.shelf_destination_remote_library
        rows = connection.execute(
            sa.select(
                d.c.id, d.c.adopted, d.c.kavita_library_type, s.c.name, link.c.remote_library_id
            )
            .select_from(
                d.join(s, s.c.id == d.c.shelf_id).join(link, link.c.shelf_destination_id == d.c.id)
            )
            .where(d.c.target_id == target_id)
            .order_by(d.c.id)
        ).all()
        found: dict[str, _Wanted] = {}
        for row in rows:
            ctx = load_context(connection, row.id)
            if ctx.root_local is None:
                continue
            root = ctx.remote_folder(ctx.root_local)
            # one library per remote_library row; the shallowest root of its destinations
            previous = found.get(row.remote_library_id)
            if previous is None or len(root) < len(previous.remote_root):
                found[row.remote_library_id] = _Wanted(
                    row.remote_library_id, root, row.name, row.kavita_library_type,
                    bool(row.adopted),
                )  # fmt: skip
        return list(found.values())

    @staticmethod
    def _store_refs(
        connection: sa.Connection, target_id: str, resolved: dict[str, str]
    ) -> dict[str, tuple[str, str]]:
        t = export.remote_library
        old = {
            r.id: r.remote_ref
            for r in connection.execute(
                sa.select(t.c.id, t.c.remote_ref).where(t.c.export_target_id == target_id)
            )
        }
        changed = {k: (old[k], v) for k, v in resolved.items() if k in old and old[k] != v}
        for library_id in changed:  # two steps: the (target, remote_ref) pair is unique
            connection.execute(
                sa.update(t).where(t.c.id == library_id).values(remote_ref=_TEMP + library_id)
            )
        for library_id, (_, new) in changed.items():
            connection.execute(sa.update(t).where(t.c.id == library_id).values(remote_ref=new))
        return changed


@dataclass(frozen=True)
class _Wanted:
    remote_library_id: str
    remote_root: str
    shelf_name: str
    library_type: str | None
    adopted: bool
