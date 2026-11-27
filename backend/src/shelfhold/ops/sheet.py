"""The series sheet (M3 L43): what the admin sees of one followed Edition, and the three settings
that had no door yet.

`sheet` reads everything in one transaction: the ranked bindings with the reason of an inactive
one, the gaps nobody acknowledged, the teams in the order of their priority, the fields and which
are locked, the cover, the merges one can still undo and the attach that waits for a decision. It
also says which actions the server allows now: the screen shows an action only when it is true.

The writes are plain settings, not reserved actions (the admin's session, its CSRF):
`reorder` ranks the active bindings of the Edition (the rank stays unique among them), the
`upstream_status` is set by hand (D23: no source writes it yet, `manual` says who),
`prefer_edition` makes an Edition the preferred one of its Work and carries each profile's reading
over (M4 L15, EF-BIB-026), and `prefer_team` puts a Team first in the priority list of the
Edition. Choosing a team replaces nothing already served: that is an upgrade, and an upgrade is
made under intent elsewhere.
Everything else the sheet does goes through the doors that already exist (`follow check`,
`follow gap-ack`, field locks, "Séparer", `edition attach-confirm`).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from shelfhold.catalog.public import (
    COVER_FIELD,
    MERGE_WINDOW_DAYS,
    Catalog,
    contributor_names,
    relations_of,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, storage
from shelfhold.library.public import cover_hash
from shelfhold.ops.errors import InvalidChoiceError, UnknownSubjectError
from shelfhold.progress_lease import build_progress_lease
from shelfhold.reading.public import edition_progress, prefer_edition

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["UPSTREAM_STATUSES", "SheetOps"]

UPSTREAM_STATUSES: Final = ("ongoing", "finished", "paused", "unknown")
_READABLE: Final = (
    "cbz",
    "epub",
)  # the formats the reader can open (reading/application/publication)
_FIELDS: Final = ("title", "description", "series_name")  # what the sheet shows and lets one edit


class SheetOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _edition(self, connection: sa.Connection, edition_id: str) -> Any:
        if not is_ulid(edition_id):
            raise InvalidChoiceError("not an Edition identifier")
        ed, work = catalog.edition.c, catalog.work.c
        row = connection.execute(
            sa.select(
                ed.id, ed.work_id, ed.lang, ed.upstream_status, ed.upstream_status_by, ed.shelf_id,
                ed.created_at, ed.published_on, ed.author_death_year,
                work.title, work.kind, work.description, work.first_published,
            ).select_from(catalog.edition.join(catalog.work, work.id == ed.work_id))
            .where(ed.id == edition_id)
        ).first()  # fmt: skip
        if row is None:
            raise UnknownSubjectError("no such Edition")
        return row

    def sheet(self, edition_id: str, viewer_id: str | None = None) -> dict[str, Any]:
        now = self._comp.clock.now()
        with self._comp.engine.connect() as connection:
            edition = self._edition(connection, edition_id)
            work_id = str(edition.work_id)
            bindings = self._bindings(connection, edition_id)
            gaps = self._gaps(connection, edition_id)
            teams = self._teams(connection, edition_id)
            follow = connection.execute(
                sa.select(acquisition.series_follow.c.check_override).where(
                    acquisition.series_follow.c.edition_id == edition_id
                )
            ).first()
            merges = self._merges(connection, work_id, now)
            attach = self._attach(connection, edition_id)
            fields = self._fields(connection, edition_id, work_id)
            cover = self._cover(connection, edition_id)
            relations = self._relations(connection, work_id)
            first_unit = self._first_unit(connection, edition_id)
            facts = self._facts(connection, edition, fields)
            mine = edition_progress(connection, viewer_id, [edition_id]) if viewer_id else {}
        paused = follow is not None and follow.check_override == "paused"
        active = [b for b in bindings if b["active"]]
        return {
            "edition_id": str(edition.id), "work_id": work_id,
            "title": " ".join(str(edition.title).split()), "lang": str(edition.lang),
            "shelf_id": edition.shelf_id, "cover_hash": cover,
            "upstream_status": str(edition.upstream_status),
            "upstream_status_by": edition.upstream_status_by,
            "followed": follow is not None, "bindings": bindings, "gaps": gaps,
            "teams": teams, "fields": fields, "merges": merges, "attach": attach,
            "relations": relations, "first_unit_id": first_unit, **facts,
            "progress": asdict(mine[edition_id]) if edition_id in mine else None,
            "can": {
                "check": follow is not None and not paused and bool(active),
                "reorder": len(active) > 1,
                "acknowledge_gap": bool(gaps),
                "set_upstream_status": True,
                "prefer_team": len(teams) > 1,
                "edit_field": True,
                "separate": bool(merges),
                "confirm_attach": attach is not None,
            },
        }  # fmt: skip

    def _facts(
        self, connection: sa.Connection, edition: Any, fields: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """What the Work and its Edition say of themselves, read only (API-1): the type, the
        authors, the dates, the summary, the number of chapters, the shelf and how it came. Each
        is empty or None when nobody said it; nothing is derived from a guess."""
        edition_id, work_id = str(edition.id), str(edition.work_id)
        shelf = None
        if edition.shelf_id is not None:
            shelf = connection.execute(
                sa.select(export.shelf.c.name).where(export.shelf.c.id == edition.shelf_id)
            ).scalar()
        units = connection.execute(
            sa.select(sa.func.count()).where(
                catalog.unit.c.edition_id == edition_id, catalog.unit.c.track == "main"
            )
        ).scalar_one()
        source = connection.execute(
            sa.select(acquisition.source_item.c.source_key)
            .select_from(
                acquisition.source_binding.join(
                    acquisition.source_item,
                    acquisition.source_item.c.id == acquisition.source_binding.c.source_item_id,
                )
            )
            .where(acquisition.source_binding.c.edition_id == edition_id)
            .order_by(acquisition.source_binding.c.created_at, acquisition.source_binding.c.id)
            .limit(1)
        ).scalar()
        described = [
            f["value"] for f in fields if f["field"] == "description" and f["value"].strip()
        ]
        summary = (edition.description or "").strip() or (described[0] if described else None)
        return {
            "kind": str(edition.kind),
            "authors": contributor_names(connection, work_id, edition_id),
            "first_published": edition.first_published,
            "published_on": edition.published_on,
            "author_death_year": edition.author_death_year,
            "summary": summary,
            "unit_count": int(units),
            "shelf_name": None if shelf is None else str(shelf),
            "added_at": edition.created_at,
            "added_source": None if source is None else str(source),
        }

    def _bindings(self, connection: sa.Connection, edition_id: str) -> list[dict[str, Any]]:
        b, s = acquisition.source_binding.c, acquisition.source_item.c
        rows = connection.execute(
            sa.select(
                b.id, b.priority, b.active, b.disabled_reason, b.last_checked, b.next_expected,
                s.source_key,
            ).select_from(
                acquisition.source_binding.join(acquisition.source_item, s.id == b.source_item_id)
            ).where(b.edition_id == edition_id)
            .order_by(b.active.desc(), b.priority, b.id)
        ).all()  # fmt: skip
        return [
            {"id": str(r.id), "source_key": str(r.source_key), "rank": int(r.priority),
             "active": bool(r.active), "disabled_reason": r.disabled_reason,
             "last_checked": r.last_checked, "next_expected": r.next_expected}
            for r in rows
        ]  # fmt: skip

    def _gaps(self, connection: sa.Connection, edition_id: str) -> list[dict[str, str]]:
        gap, ack = acquisition.series_gap.c, acquisition.gap_ack.c
        acknowledged = (
            sa.select(sa.literal(1))
            .where(
                ack.edition_id == gap.edition_id, ack.track == gap.track,
                ack.number_segment == gap.number_segment, ack.number_norm == gap.number_norm,
            )
            .exists()
        )  # fmt: skip
        rows = connection.execute(
            sa.select(gap.track, gap.number_segment, gap.number_norm).where(
                gap.edition_id == edition_id, ~acknowledged
            )
        ).all()
        found = [
            {"track": str(r.track), "segment": str(r.number_segment), "number": str(r.number_norm)}
            for r in rows
        ]
        return sorted(found, key=lambda g: (g["track"], g["segment"], int(g["number"])))

    def _teams(self, connection: sa.Connection, edition_id: str) -> list[dict[str, Any]]:
        team, priority = catalog.team.c, catalog.team_priority.c
        ranked = {
            str(r.team_id): int(r.rank)
            for r in connection.execute(
                sa.select(priority.team_id, priority.rank).where(priority.edition_id == edition_id)
            )
        }
        rows = connection.execute(
            sa.select(team.id, team.label).where(team.edition_id == edition_id)
        ).all()
        listed: list[dict[str, Any]] = [
            {"id": str(r.id), "label": str(r.label), "rank": ranked.get(str(r.id))} for r in rows
        ]
        listed.sort(key=lambda t: (t["rank"] is None, t["rank"] or 0, t["label"], t["id"]))
        for place, found in enumerate(listed):
            found["preferred"] = place == 0 and found["rank"] is not None
        return listed

    def _fields(
        self, connection: sa.Connection, edition_id: str, work_id: str
    ) -> list[dict[str, Any]]:
        t = catalog.metadata_field.c
        rows = connection.execute(
            sa.select(t.entity_type, t.field, t.lang, t.value, t.provenance, t.locked).where(
                sa.or_(
                    sa.and_(t.entity_type == "edition", t.entity_id == edition_id),
                    sa.and_(t.entity_type == "work", t.entity_id == work_id),
                ),
                t.field.in_(_FIELDS),
            ).order_by(t.entity_type, t.field, t.lang)
        ).all()  # fmt: skip
        found: list[dict[str, Any]] = []
        for r in rows:
            try:
                value = json.loads(r.value)
            except ValueError:
                continue
            if isinstance(value, str):
                found.append({
                    "on": str(r.entity_type), "field": str(r.field), "lang": str(r.lang),
                    "value": value, "manual": r.provenance == "manual", "locked": bool(r.locked),
                })  # fmt: skip
        return found

    def _relations(self, connection: sa.Connection, work_id: str) -> list[dict[str, Any]]:
        """The relations of the Work, all of them: the sheet is the admin's (EF-BIB-039). Each is
        read from this Work's side, with the other Work's title and its first Edition, where its
        own sheet is."""
        related = relations_of(connection, work_id)
        if not related:
            return []
        others = sorted({r.other_work_id for r in related})
        titles = {
            str(r.id): str(r.title)
            for r in connection.execute(
                sa.select(catalog.work.c.id, catalog.work.c.title).where(
                    catalog.work.c.id.in_(others)
                )
            )
        }
        editions: dict[str, str] = {}
        for row in connection.execute(
            sa.select(catalog.edition.c.work_id, catalog.edition.c.id)
            .where(catalog.edition.c.work_id.in_(others))
            .order_by(catalog.edition.c.id.desc())
        ):
            editions[str(row.work_id)] = str(row.id)  # the oldest Edition ends up the one kept
        return [
            {"work_id": r.other_work_id, "title": titles.get(r.other_work_id, ""), "kind": r.kind,
             "outgoing": r.outgoing, "edition_id": editions.get(r.other_work_id)}
            for r in related
        ]  # fmt: skip

    def _first_unit(self, connection: sa.Connection, edition_id: str) -> str | None:
        """The Unit the "Lire" button opens (M5 L10): the first, by number, that has a file. Which
        file is read, and whether in full or as an excerpt, is the publication route's choice."""
        u = catalog.unit.c
        row = connection.execute(
            sa.select(u.id)
            .where(
                u.edition_id == edition_id,
                sa.exists().where(
                    storage.content.c.unit_id == u.id,
                    storage.content.c.format.in_(_READABLE),
                ),
            )
            .order_by(
                u.number_sort.is_(None),
                u.number_sort,
                u.number_segment_ordinal,
                u.source_order,
                u.id,
            )
            .limit(1)
        ).first()
        return None if row is None else str(row.id)

    def _cover(self, connection: sa.Connection, edition_id: str) -> str | None:
        t = catalog.metadata_field.c
        row = connection.execute(
            sa.select(t.value).where(
                t.entity_type == "edition", t.entity_id == edition_id, t.field == COVER_FIELD
            )
        ).first()
        if row is None:
            return None
        try:
            ref = json.loads(row.value)
        except ValueError:
            return None
        return cover_hash(ref) if isinstance(ref, str) else None

    def _merges(
        self, connection: sa.Connection, work_id: str, now: datetime
    ) -> list[dict[str, Any]]:
        log = catalog.merge_log.c
        rows = connection.execute(
            sa.select(log.id, log.created_at).where(
                log.kind == "merge", log.survivor_id == work_id, log.undone_at.is_(None)
            ).order_by(log.created_at.desc())
        ).all()  # fmt: skip
        window = timedelta(days=MERGE_WINDOW_DAYS)
        return [
            {"merge_id": str(r.id), "merged_at": r.created_at,
             "undoable_until": r.created_at + window}
            for r in rows if now - r.created_at <= window
        ]  # fmt: skip

    def _attach(self, connection: sa.Connection, edition_id: str) -> dict[str, str] | None:
        t = catalog.edition_attach.c
        row = connection.execute(
            sa.select(t.id, t.from_work_id, t.to_work_id).where(
                t.edition_id == edition_id, t.state == "suggested"
            ).order_by(t.id).limit(1)
        ).first()  # fmt: skip
        if row is None:
            return None
        return {"attach_id": str(row.id), "from_work_id": str(row.from_work_id),
                "to_work_id": str(row.to_work_id)}  # fmt: skip

    # -- the writes -----------------------------------------------------------------------------
    def reorder(self, edition_id: str, binding_ids: list[str]) -> dict[str, Any]:
        """Rank the active bindings in the order given. The list must be exactly the active
        bindings of this Edition: one missing, repeated or foreign is refused and nothing moves."""
        b = acquisition.source_binding
        with write_transaction(self._comp.engine) as connection:
            self._edition(connection, edition_id)
            active = {
                str(i)
                for i in connection.execute(
                    sa.select(b.c.id).where(b.c.edition_id == edition_id, b.c.active.is_(True))
                ).scalars()
            }
            if len(set(binding_ids)) != len(binding_ids) or set(binding_ids) != active:
                raise InvalidChoiceError("every active binding of the Edition, once")
            top = (
                connection.execute(
                    sa.select(sa.func.max(b.c.priority)).where(b.c.edition_id == edition_id)
                ).scalar()
                or 0
            )
            # two steps: the rank is unique among the active bindings at every moment
            for place, binding_id in enumerate(binding_ids, start=1):
                connection.execute(
                    sa.update(b).where(b.c.id == binding_id).values(priority=top + place)
                )
            for place, binding_id in enumerate(binding_ids, start=1):
                connection.execute(sa.update(b).where(b.c.id == binding_id).values(priority=place))
        return {"edition_id": edition_id, "order": binding_ids}

    def set_upstream_status(self, edition_id: str, status: str) -> dict[str, Any]:
        if status not in UPSTREAM_STATUSES:
            raise InvalidChoiceError("ongoing, finished, paused or unknown")
        ed = catalog.edition
        with write_transaction(self._comp.engine) as connection:
            self._edition(connection, edition_id)
            connection.execute(
                sa.update(ed).where(ed.c.id == edition_id).values(
                    upstream_status=status, upstream_status_by="manual",
                    upstream_status_at=self._comp.clock.now(),
                )
            )  # fmt: skip
        return {"edition_id": edition_id, "upstream_status": status, "by": "manual"}

    def prefer_edition(self, edition_id: str) -> dict[str, Any]:
        """Make the Edition the preferred one of its Work, and carry each profile's reading over
        to it (EF-BIB-026). The answer counts profiles; it never says whose reading."""
        with self._comp.engine.connect() as peek:
            work_id = str(self._edition(peek, edition_id).work_id)
        # the carry over writes a position and read marks per profile: the lease of the Work, like
        # every other writer of a position (M5 L22a); held by a job, `ProgressBusyError` (a 503)
        with (
            build_progress_lease(self._comp.engine, self._comp.clock)(work_id),
            write_transaction(self._comp.engine) as connection,
        ):
            edition = self._edition(connection, edition_id)
            done = prefer_edition(connection, edition_id=edition_id, now=self._comp.clock.now())
        return {
            "edition_id": edition_id, "work_id": str(edition.work_id),
            "transferred": done.transferred, "pending": done.pending,
        }  # fmt: skip

    def prefer_team(self, edition_id: str, team_id: str) -> dict[str, Any]:
        if not is_ulid(team_id):
            raise InvalidChoiceError("not a Team identifier")
        with write_transaction(self._comp.engine) as connection:
            self._edition(connection, edition_id)
            known = connection.execute(
                sa.select(catalog.team.c.id).where(
                    catalog.team.c.id == team_id, catalog.team.c.edition_id == edition_id
                )
            ).first()
            if known is None:
                raise UnknownSubjectError("no such Team on this Edition")
            Catalog(connection).prefer_team(edition_id, team_id)
        return {"edition_id": edition_id, "team_id": team_id}
