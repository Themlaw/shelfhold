"""L14 (EF-BIB-053 (1), EF-BIB-054): a purged Work leaves a `work` tombstone on every external
identifier it carried, and an Edition that arrives with one of them is not attached, not recorded
and not given the Work's fields: a purged (or merged) Work does not come back by an identifier.
Real purge, real cipher, real journal; the catalog asks the tombstones through the screen the
host gives it."""

import json
from dataclasses import dataclass

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    AttachRefusedError,
    AutoAttachService,
    ExternalId,
    IncomingEdition,
    TombstoneScreenMissingError,
    add_external_id,
    renumber_external_id,
)
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, platform
from shelfhold.library.public import CatalogScreenAdapter, WorkRef
from shelfhold.library.tombstones import TombstoneService
from shelfhold.sdk.metadata import FieldProposal, Provenance
from tests.export_env import ExportEnv, Series
from tests.family_env import NOW, family_env, rate, rate_edition, suggest
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_work,
)
from tests.purge_env import ADMIN, PurgeEnv, make_purge_env

pytestmark = pytest.mark.req("EF-BIB-053", "EF-BIB-054")

ANILIST = ExternalId("work", "anilist", "30013")
RENUMBERED = ExternalId("work", "mangaupdates", "55512")
EDITION_ID = ExternalId("edition", "kitsu", "ed-778")
SECRET_VALUES = ("30013", "55512", "ed-778", "88801")


@dataclass
class World:
    env: ExportEnv
    purge: PurgeEnv
    screen: CatalogScreenAdapter
    service: AutoAttachService
    series: Series  # the Work that carries the identifiers


def world(env: ExportEnv) -> World:
    purge = make_purge_env(env.lib)
    series = env.new_series("Dr Stone")
    env.add_chapter(series, "1")
    with env.engine.begin() as connection:
        add_external_id(connection, series.work_id, ANILIST, origin="provider")
        add_external_id(
            connection, series.work_id, ExternalId("work", "mangaupdates", "1"), origin="provider"
        )
        renumber_external_id(
            connection,
            series.work_id,
            ExternalId("work", "mangaupdates", "1"),
            "55512",
            origin="provider",
        )
        add_external_id(connection, series.edition_id, EDITION_ID, origin="provider")
    screen = CatalogScreenAdapter(purge.tombstones)
    service = AutoAttachService(events=EventBus(env.clock), tombstones=screen)
    return World(env, purge, screen, service, series)


def purge_the_work(w: World) -> None:
    w.purge.purge.purge_work(
        w.series.work_id, principal=ADMIN, typed=w.series.work_id, now=w.purge.clock.now()
    )


def arriving(env: ExportEnv, title: str = "Dr Stone (uncut)") -> Series:
    with env.engine.begin() as connection:
        work = add_work(connection, title=title, sort_title=title.lower())
        edition = add_edition(connection, work, lang="en")
        item = add_source_item(connection, remote_id=f"series-{new_ulid()}")
        binding = add_binding(connection, work, edition, item)
    return Series(work, edition, binding)


def arrive(w: World, incoming: IncomingEdition) -> object:
    with w.env.engine.begin() as connection:
        return w.service.arrive(connection, incoming, now=NOW)


def external_rows(env: ExportEnv) -> list[tuple[object, ...]]:
    with env.engine.connect() as connection:
        return sorted(tuple(r) for r in connection.execute(sa.select(catalog.external_id)))


def blocked(w: World, provider: str, value: str) -> bool:
    return w.purge.tombstones.check(WorkRef(provider, value), root_id=None).blocked


@pytest.mark.req("EF-BIB-053")
def test_purge_work_lays_a_work_tombstone_per_external_id() -> None:
    with family_env(13) as env:
        w = world(env)
        assert not blocked(w, "anilist", "30013")  # nothing before the purge
        purge_the_work(w)
        # the identifier, the one a renumbering marked superseded and the one that replaced it
        assert blocked(w, "anilist", "30013")
        assert blocked(w, "mangaupdates", "1") and blocked(w, "mangaupdates", "55512")
        # what it did not carry, and what is not a Work's identifier, is not covered
        assert not blocked(w, "anilist", "99999")
        assert not blocked(w, "kitsu", "ed-778")


@pytest.mark.req("EF-BIB-053")
def test_purge_of_a_unit_lays_no_work_tombstone() -> None:
    with family_env(13) as env:
        w = world(env)
        unit = env.add_chapter(w.series, "2")
        w.purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=w.purge.clock.now())
        assert not blocked(w, "anilist", "30013")  # the Work lives on


@pytest.mark.req("EF-BIB-053", "EF-BIB-054")
def test_work_tombstone_holds_no_readable_value() -> None:
    """Fin du lot: nothing readable in the audit, the events, the table or the journals."""
    with family_env(13) as env:
        w = world(env)
        purge_the_work(w)
        w.screen.work_ids([ANILIST])
        w.screen.announce()  # the hit is told: the scope only
        dump = w.purge.dump()
        journals = b"".join(w.purge.journal(r) for r in (env.lib.library, env.lib.store))
        for value in SECRET_VALUES[:2]:
            assert value not in dump
            assert value.encode() not in journals
        with env.engine.connect() as connection:
            payloads = [
                json.dumps(r.payload)
                for r in connection.execute(sa.select(platform.event.c.payload))
            ]
        assert not [p for p in payloads if "30013" in p or "anilist" in p]


@pytest.mark.req("EF-BIB-053", "EF-BIB-054")
def test_work_tombstone_blocks_by_external_id() -> None:
    """An Edition that arrives with the identifier of a purged Work is attached to nothing: the
    identifier is not recorded for its Work, its Work gets none of the fields it came with."""
    with family_env(13) as env:
        w = world(env)
        purge_the_work(w)
        again = arriving(env)
        before = external_rows(env)
        arrival = arrive(
            w,
            IncomingEdition(
                again.edition_id, ids=(ANILIST,),
                fields=(FieldProposal(
                    entity_type="work", field="summary", value="the old summary",
                    confidence=0.9, provenance=Provenance(provider="anilist", fetched_at=NOW),
                ),),
            ),
        )  # fmt: skip
        assert arrival.state == "conflict" and "tombstoned" in arrival.conflicts  # type: ignore[attr-defined]
        assert arrival.work_id == again.work_id  # type: ignore[attr-defined]
        assert external_rows(env) == before  # not recorded for the new Work
        with env.engine.connect() as connection:
            assert connection.execute(
                sa.select(catalog.work.c.id).where(catalog.work.c.id == again.work_id)
            ).first()
            fields = connection.execute(sa.text("SELECT count(*) FROM metadata_field")).scalar()
        assert fields == 0


@pytest.mark.req("EF-BIB-053", "EF-BIB-054")
def test_work_tombstone_blocks_a_renumbered_identifier_too() -> None:
    with family_env(13) as env:
        w = world(env)
        purge_the_work(w)
        again = arriving(env)
        arrival = arrive(
            w, IncomingEdition(again.edition_id, ids=(ExternalId("work", "mangaupdates", "1"),))
        )
        assert arrival.state == "conflict"  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-053")
def test_one_tombstoned_identifier_among_free_ones_blocks_the_whole_arrival() -> None:
    with family_env(13) as env:
        w = world(env)
        purge_the_work(w)
        again = arriving(env)
        free = ExternalId("work", "kitsu", "88801")
        arrival = arrive(w, IncomingEdition(again.edition_id, ids=(free, ANILIST)))
        assert arrival.state == "conflict"  # type: ignore[attr-defined]
        assert not any(r[3] == "88801" for r in external_rows(env))  # the free one waits too


@pytest.mark.req("EF-BIB-053")
def test_a_live_work_holding_a_tombstoned_identifier_attracts_nothing() -> None:
    """A tombstone outranks a holder: an older restore may have put the Work back with its ids."""
    with family_env(13) as env:
        w = world(env)
        live = env.new_series("Resurrected")
        kitsu = ExternalId("work", "kitsu", "88801")
        with env.engine.begin() as connection:
            add_external_id(connection, live.work_id, kitsu, origin="provider")
        w.purge.tombstones.record(WorkRef("kitsu", "88801"), w.purge.roots, reason="purge_work")
        again = arriving(env)
        before = external_rows(env)
        arrival = arrive(w, IncomingEdition(again.edition_id, ids=(kitsu,)))
        assert arrival.state == "conflict" and arrival.work_id == again.work_id  # type: ignore[attr-defined]
        assert external_rows(env) == before


@pytest.mark.req("EF-BIB-053")
def test_work_tombstone_survives_a_restore_to_an_older_database() -> None:
    """The table is what a restore rewinds; the journal outside the database is what remembers.
    After the replay the identifier is blocked again, and written back to the table."""
    with family_env(13) as env:
        w = world(env)
        purge_the_work(w)
        with env.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM content_tombstone WHERE scope = 'work'"))
        fresh = TombstoneService(
            env.engine, w.purge.store.load(), env.lib.fsops, EventBus(env.clock)
        )
        fresh.replay(w.purge.roots)
        assert fresh.check(WorkRef("anilist", "30013"), root_id=None).blocked
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM content_tombstone WHERE scope = 'work'")
                ).scalar()
                >= 3
            )


@pytest.mark.req("EF-BIB-053")
def test_an_import_does_not_clear_a_work_tombstone() -> None:
    """Arriving again and again (an import, a follow cycle) neither records the identifier nor
    removes the tombstone: it blocks the third time as the first."""
    with family_env(13) as env:
        w = world(env)
        purge_the_work(w)
        for _ in range(3):
            again = arriving(env, f"Again {_}")
            arrival = arrive(w, IncomingEdition(again.edition_id, ids=(ANILIST,)))
            assert arrival.state == "conflict"  # type: ignore[attr-defined]
        assert blocked(w, "anilist", "30013")
        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT count(*) FROM content_tombstone WHERE scope = 'work'")
            ).scalar()
        assert rows == 3  # one per identifier, laid once


@pytest.mark.req("EF-BIB-053")
def test_an_arrival_without_a_screen_fails_closed() -> None:
    with family_env(13) as env:
        again = arriving(env)
        service = AutoAttachService(events=EventBus(env.clock))
        with env.engine.begin() as connection, pytest.raises(TombstoneScreenMissingError):
            service.arrive(connection, IncomingEdition(again.edition_id, ids=(ANILIST,)), now=NOW)


@pytest.mark.req("EF-BIB-053")
def test_a_suggested_attach_is_refused_once_its_identifier_is_tombstoned() -> None:
    """A suggestion waits for an admin; the Work it names may be purged meanwhile."""
    with family_env(13) as env:
        w = world(env)
        holder = env.new_series("Holder")
        rate(env, holder, 0)
        with env.engine.begin() as connection:
            add_external_id(
                connection, holder.work_id, ExternalId("work", "kitsu", "88801"), origin="provider"
            )
        uncut = arriving(env)
        rate_edition(env, uncut.edition_id, 18)
        suggest(env, uncut.edition_id, 18)
        arrival = arrive(
            w,
            IncomingEdition(
                uncut.edition_id, shelf_id=env.shelf_id, ids=(ExternalId("work", "kitsu", "88801"),)
            ),
        )
        assert arrival.state == "suggested"  # type: ignore[attr-defined]
        w.purge.tombstones.record(WorkRef("kitsu", "88801"), w.purge.roots, reason="purge_work")
        with (
            env.engine.begin() as connection,
            pytest.raises(AttachRefusedError, match="tombstoned"),
        ):
            w.service.plan(connection, arrival.attach_id)  # type: ignore[attr-defined]


@pytest.mark.req("EF-BIB-054")
@pytest.mark.parametrize(
    ("provider", "stored", "again"),
    [
        ("wikidata", "Q42", "q42"),
        ("openlibrary", "OL123W", "/works/OL123W"),
        ("anilist", "30013", "030013"),
    ],
)
def test_work_tombstone_compares_under_a_versioned_normalizer(
    provider: str, stored: str, again: str
) -> None:
    """Review PoC: a spelling the provider accepts for the same identifier does not escape."""
    with family_env(13) as env:
        w = world(env)
        with env.engine.begin() as connection:
            add_external_id(
                connection,
                w.series.work_id,
                ExternalId("work", provider, stored),
                origin="provider",
            )
        purge_the_work(w)
        new = arriving(env)
        got = arrive(w, IncomingEdition(new.edition_id, ids=(ExternalId("work", provider, again),)))
        assert got.state == "conflict"  # type: ignore[attr-defined]
        other = arriving(env, "Other")
        free = arrive(
            w, IncomingEdition(other.edition_id, ids=(ExternalId("work", provider, "999"),))
        )
        assert free.state != "conflict"  # type: ignore[attr-defined]
