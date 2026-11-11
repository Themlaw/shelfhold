"""L12 (EF-BIB-003, EF-BIB-037, EF-BIB-055): attaching an Edition by external identifier.

An Edition that arrives with the identifier another Work holds (the same AniList id) is attached to
that Work: the attach is shown (an event, the list of attaches) and undone by "Séparer"
(`edition detach`), which puts back exactly what it changed: the Work, the rating stamp and the
shelf of the Edition, the values, provenances and locks of the fields, and nothing in the external
identifiers, which an attach never writes. An attach that would raise the effective age is never
automatic: it is a suggestion, and its confirmation shows the removal plan of the family shelf
before anything moves. The new Edition goes through the gate first: a shelf below 18+ never takes
it until an admin rates it.
"""

from dataclasses import dataclass
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import (
    Admission,
    AttachPlanStaleError,
    AttachRefusedError,
    AutoAttachService,
    ExternalId,
    FieldKey,
    IncomingEdition,
    Withdrawal,
    add_external_id,
    lock_field,
    propose_fields,
    suggest_age,
)
from shelfhold.events.bus import EventBus
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.to_rate import to_rate
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from shelfhold.sdk.age import AgeRating
from shelfhold.sdk.metadata import FieldProposal, Provenance
from tests.export_env import ExportEnv, Series
from tests.family_env import (
    NOW,
    add_adult_shelf,
    admin,
    family_env,
    rate,
    rate_edition,
    suggest,
)
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_work,
)

pytestmark = pytest.mark.req("EF-BIB-055", "EF-BIB-037", "EF-BIB-003")

ANILIST = ExternalId("work", "anilist", "30013")


def proposal(field: str, value: str, *, by: str = "anilist") -> FieldProposal:
    return FieldProposal(
        entity_type="work", field=field, value=value, confidence=0.8,
        provenance=Provenance(provider=by, fetched_at=NOW),
    )  # fmt: skip


@dataclass
class Scene:
    env: ExportEnv
    bus: EventBus
    service: AutoAttachService
    family: Series  # the Work "Tous publics" on the family shelf, holder of the AniList id


def scene(env: ExportEnv) -> Scene:
    family = env.new_series("Dr Stone")
    env.add_chapter(family, "1")
    rate(env, family, 0)
    with env.engine.begin() as connection:
        add_external_id(connection, family.work_id, ANILIST, origin="provider")
        lock_field(connection, FieldKey("work", family.work_id, "title"), "Dr Stone", now=NOW)
        propose_fields(
            connection, {"work": family.work_id}, [proposal("summary", "old", by="kitsu")],
            now=NOW - timedelta(days=3),
        )  # fmt: skip
    bus = EventBus(env.clock)
    return Scene(env, bus, AutoAttachService(events=bus), family)


def arriving(env: ExportEnv, *, title: str = "Dr Stone (uncut)") -> Series:
    """The ingest of a source made a Work and an Edition for what it brought, filed nowhere."""
    with env.engine.begin() as connection:
        work = add_work(connection, title=title, sort_title=title.lower())
        edition = add_edition(connection, work, lang="en")
        item = add_source_item(connection, remote_id=f"series-{new_ulid()}")
        binding = add_binding(connection, work, edition, item)
    return Series(work, edition, binding)


def edition_row(env: ExportEnv, edition_id: str) -> tuple[object, ...]:
    e = catalog.edition
    with env.engine.connect() as connection:
        return tuple(
            connection.execute(
                sa.select(e.c.work_id, e.c.rated_at, e.c.shelf_id).where(e.c.id == edition_id)
            ).one()
        )


def table_rows(env: ExportEnv, table: sa.Table) -> list[tuple[object, ...]]:
    with env.engine.connect() as connection:
        return sorted(tuple(r) for r in connection.execute(sa.select(table)))


def events(env: ExportEnv) -> list[tuple[str, dict[str, object]]]:
    with env.engine.connect() as connection:
        return [
            (r.type, dict(r.payload))
            for r in connection.execute(
                sa.select(platform.event.c.type, platform.event.c.payload).order_by(
                    platform.event.c.id
                )
            )
        ]


def run(env: ExportEnv) -> int:
    return len(env.pipeline.run(env.destination_id, job_id=new_ulid()).ops)


def arrive(s: Scene, incoming: IncomingEdition) -> object:
    with s.env.engine.begin() as connection:
        return s.service.arrive(connection, incoming, now=NOW)


def test_unrated_new_edition_never_enters_family_shelf() -> None:
    """Cas du rattachement automatique: an uncensored Edition shares the AniList id of a Work
    "Tous publics" on the family shelf, with a suggested age of 18+."""
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        suggest(env, uncut.edition_id, 18)  # the source said so
        arrival = arrive(
            s, IncomingEdition(uncut.edition_id, shelf_id=env.shelf_id, ids=(ANILIST,))
        )
        assert arrival.state == "attached"  # type: ignore[attr-defined]
        assert edition_row(env, uncut.edition_id) == (s.family.work_id, None, None)
        with env.engine.connect() as connection:
            assert s.family.work_id in [r.work_id for r in to_rate(connection)]
        attached = Series(s.family.work_id, uncut.edition_id, uncut.binding_id)
        env.add_chapter(attached, "1")
        assert run(env) == 1  # the chapter of the rated Edition alone
        rate(env, s.family, 0)  # rating the Work again files nothing by itself
        assert edition_row(env, uncut.edition_id)[2] is None
        assert run(env) == 0
        rate_edition(env, uncut.edition_id, 0)  # the admin rules on this Edition
        assert (
            ShelfService(env.engine, clock=env.clock)
            .file_edition(uncut.edition_id, env.shelf_id)
            .filed
        )
        assert run(env) == 1


def test_higher_suggested_age_blocks_edition() -> None:
    """Cas du fournisseur: an Edition rated with its Work, then a provider suggests X."""
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        with env.engine.begin() as connection:
            plan = suggest_age(
                connection, "edition", series.edition_id, AgeRating(18, x=True),
                provenance="provider", now=NOW,
            )  # fmt: skip
            again = suggest_age(
                connection, "edition", series.edition_id, AgeRating(18, x=True),
                provenance="provider", now=NOW,
            )  # fmt: skip
        assert (
            plan
            == again
            == (Withdrawal(series.edition_id, env.shelf_id, Admission.SUGGESTION_ABOVE),)
        )
        blocked = {b.code for b in env.pipeline.preview(env.destination_id).blocked}
        assert blocked == {"shelf.suggestion_above"}
        assert run(env) == 0 and env.files() == []


def test_auto_attach_displayed_and_undoable() -> None:
    with family_env(13) as env:
        s = scene(env)
        adult_shelf, _ = add_adult_shelf(env)
        uncut = arriving(env)
        rate(env, uncut, 16)  # it had been rated where it came from
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == uncut.edition_id)
                .values(shelf_id=adult_shelf)
            )
        before_edition = edition_row(env, uncut.edition_id)
        before_ids = table_rows(env, catalog.external_id)
        before_fields = table_rows(env, catalog.metadata_field)
        before_binding = table_rows(env, acquisition.source_binding)
        incoming = IncomingEdition(
            uncut.edition_id,
            shelf_id=adult_shelf,
            ids=(ANILIST, ExternalId("work", "mangaupdates", "m-1")),
            fields=(
                proposal("title", "Uncut"),
                proposal("summary", "new"),
                proposal("genre", "sf"),
            ),
        )
        arrival = arrive(s, incoming)
        attach_id = arrival.attach_id  # type: ignore[attr-defined]
        assert arrival.state == "attached"  # type: ignore[attr-defined]
        # displayed: an event and the list of attaches
        assert events(env)[-1] == (
            "edition.auto_attached",
            {
                "attach_id": attach_id, "edition_id": uncut.edition_id,
                "work_id": s.family.work_id, "from_work_id": uncut.work_id,
            },
        )  # fmt: skip
        with env.engine.connect() as connection:
            (shown,) = s.service.attachments(connection)
        assert (shown.attach_id, shown.state, shown.edition_id) == (
            attach_id, "attached", uncut.edition_id,
        )  # fmt: skip
        # new to the Work: unrated there; the adult shelf takes it
        assert edition_row(env, uncut.edition_id) == (s.family.work_id, None, adult_shelf)
        assert table_rows(env, catalog.external_id) == before_ids  # an attach writes no id
        with env.engine.connect() as connection:
            values = dict(
                connection.execute(
                    sa.select(catalog.metadata_field.c.field, catalog.metadata_field.c.value).where(
                        catalog.metadata_field.c.entity_id == s.family.work_id
                    )
                ).all()
            )
        assert values == {"title": '"Dr Stone"', "summary": '"new"', "genre": '"sf"'}
        # "Séparer": exactly as before
        with env.engine.begin() as connection:
            undone = s.service.detach(connection, attach_id, principal=admin(), now=NOW)
        assert undone.kept == ()
        assert edition_row(env, uncut.edition_id) == before_edition
        assert table_rows(env, catalog.metadata_field) == before_fields
        assert table_rows(env, catalog.external_id) == before_ids
        assert table_rows(env, acquisition.source_binding) == before_binding
        with env.engine.connect() as connection:
            assert [a.state for a in s.service.attachments(connection)] == ["undone"]
        with env.engine.begin() as connection, pytest.raises(AttachRefusedError):
            s.service.detach(connection, attach_id, principal=admin(), now=NOW)  # once


def test_undo_keeps_what_an_admin_changed_since() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        arrival = arrive(
            s,
            IncomingEdition(
                uncut.edition_id, shelf_id=None, ids=(ANILIST,),
                fields=(proposal("summary", "new"), proposal("genre", "sf")),
            ),
        )  # fmt: skip
        with env.engine.begin() as connection:
            lock_field(connection, FieldKey("work", s.family.work_id, "genre"), "seinen", now=NOW)
            undone = s.service.detach(
                connection,
                arrival.attach_id,
                principal=admin(),
                now=NOW,  # type: ignore[attr-defined]
            )
        assert undone.kept == (FieldKey("work", s.family.work_id, "genre"),)
        with env.engine.connect() as connection:
            t = catalog.metadata_field
            rows = dict(
                connection.execute(
                    sa.select(t.c.field, sa.tuple_(t.c.value, t.c.locked)).where(
                        t.c.entity_id == s.family.work_id
                    )
                ).all()
            )
        assert rows["summary"] == ('"old"', False) and rows["genre"] == ('"seinen"', True)


def test_undo_of_an_unrated_edition_never_files_it_below_18() -> None:
    """The way back is a road too: the gate decides the shelf it returns to."""
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        with env.engine.begin() as connection:  # it waited, held, on the family shelf
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == uncut.edition_id)
                .values(shelf_id=env.shelf_id)
            )
        arrival = arrive(
            s, IncomingEdition(uncut.edition_id, shelf_id=env.shelf_id, ids=(ANILIST,))
        )
        with env.engine.begin() as connection:
            s.service.detach(
                connection,
                arrival.attach_id,
                principal=admin(),
                now=NOW,  # type: ignore[attr-defined]
            )
        assert edition_row(env, uncut.edition_id) == (uncut.work_id, None, None)


def test_age_raising_merge_by_external_id_requires_confirmation() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        rate_edition(env, uncut.edition_id, 18)  # an Edition 18+
        assert run(env) == 1  # the family Edition is in the library the children read
        arrival = arrive(
            s, IncomingEdition(uncut.edition_id, shelf_id=env.shelf_id, ids=(ANILIST,))
        )
        attach_id = arrival.attach_id  # type: ignore[attr-defined]
        assert arrival.state == "suggested"  # type: ignore[attr-defined]
        assert edition_row(env, uncut.edition_id)[0] == uncut.work_id  # nothing moved
        assert events(env)[-1][0] == "edition.attach_suggested"
        with env.engine.begin() as connection:
            plan = s.service.plan(connection, attach_id)
        # the removal plan of the family shelf, shown before the attach
        assert plan.withdrawals == (
            Withdrawal(s.family.edition_id, env.shelf_id, Admission.ABOVE_CEILING),
        )
        assert edition_row(env, uncut.edition_id)[0] == uncut.work_id  # the plan moved nothing
        assert run(env) == 0  # and it stays there: nothing was attached
        key = Principal(
            user_id=new_ulid(), username="k", role="admin", credential=Credential.API_KEY,
            session_id=None, auth_time=None, can_rate=True,
        )  # fmt: skip
        with env.engine.begin() as connection, pytest.raises(AttachRefusedError):
            s.service.confirm(connection, attach_id, confirm=plan.token, principal=key, now=NOW)
        with env.engine.begin() as connection, pytest.raises(AttachPlanStaleError):
            s.service.confirm(
                connection, attach_id, confirm="1-00000000", principal=admin(), now=NOW
            )
        with env.engine.begin() as connection:
            done = s.service.confirm(
                connection, attach_id, confirm=plan.token, principal=admin(), now=NOW
            )
        assert done.withdrawals == plan.withdrawals
        assert edition_row(env, uncut.edition_id)[0] == s.family.work_id
        blocked = {b.code for b in env.pipeline.preview(env.destination_id).blocked}
        assert "shelf.above_ceiling" in blocked


def test_arrival_without_holder_records_its_ids_on_its_own_work() -> None:
    with family_env(13) as env:
        s = scene(env)
        lone = arriving(env, title="Autre")
        other = ExternalId("work", "anilist", "777")
        arrival = arrive(s, IncomingEdition(lone.edition_id, shelf_id=None, ids=(other,)))
        assert arrival.state == "kept"  # type: ignore[attr-defined]
        assert arrival.attach_id is None  # type: ignore[attr-defined]
        with env.engine.connect() as connection:
            assert _holder(connection, other) == lone.work_id


def _holder(connection: Connection, external: ExternalId) -> str | None:
    from shelfhold.catalog.public import holder_of

    return holder_of(connection, external)
