"""EF-BIB-012, EF-BIB-037: the repair obeys the age barrier; a damaged copy is an explicit state.

A repair writes bytes into a file a shelf serves: when the shelf no longer admits the Edition the
file is not replaced, the fresh bytes wait outside the library and `status` names the exit. The
mark of a damaged copy is `replica.damaged_at`, never the absence of a verification, which an
imported copy also has.
"""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.library_repair import held_path
from shelfhold.library.adapters.export_source import export_sources
from shelfhold.library.application.integrity import check_cbz
from shelfhold.library.replica import (
    independent_copies,
    library_copies,
    readable_unit_ids,
    store_proofs,
)
from shelfhold.ops.status import collect
from tests.export_env import export_env
from tests.family_env import family_env, rate
from tests.integration.test_upgrade_age_barrier import served_on_family_shelf
from tests.integrity_env import Rig, flip_page_byte, held_repair
from tests.upgrade_env import bin_files, export_one

pytestmark = pytest.mark.req("EF-BIB-012", "EF-BIB-037")


def _waiting(env) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        return [(i.state, i.reason) for i in collect(connection) if i.state == "withdrawal_pending"]


def test_a_repair_is_held_while_the_shelf_no_longer_admits_the_edition() -> None:
    with family_env(13) as env:
        exported, edition = served_on_family_shelf(env)
        rig = Rig(env, exported)
        good = rig.received().read_bytes()
        damaged = rig.received()
        flip_page_byte(damaged)
        broken = damaged.read_bytes()
        assert rig.checker().check_unit(exported.unit_id).state == "corrupt"
        rate(env, exported.series, 16)  # the Work's age was raised: the shelf refuses it now
        env.clock.advance(days=1)

        assert rig.repair_copy(good) is False
        assert damaged.read_bytes() == broken and bin_files(env) == []  # nothing replaced
        kept = held_path(env.lib.data, exported.unit_id)
        assert kept.read_bytes() == good  # the fresh copy waits, outside any library
        assert env.lib.library not in kept.parents and env.lib.manga not in kept.parents
        assert rig.state()[0] == "corrupt"
        assert rig.events("unit.upgraded") == []
        assert any("integrity_blocked_by_age" in reason for _s, reason in _waiting(env))
        with env.engine.connect() as connection:
            (item,) = [i for i in collect(connection) if "integrity_blocked" in i.reason]
        assert (item.subject, item.command) == (edition, "shelfhold shelf withdraw")

        # still closed: the retry keeps the bytes and changes nothing
        assert held_repair(env).retry_held(exported.unit_id) is False
        assert kept.is_file() and damaged.read_bytes() == broken


def test_a_held_repair_goes_through_once_the_barrier_opens() -> None:
    with family_env(13) as env:
        exported, _edition = served_on_family_shelf(env)
        rig = Rig(env, exported)
        good = rig.received().read_bytes()
        damaged = rig.received()
        flip_page_byte(damaged)
        rig.checker().check_unit(exported.unit_id)
        rate(env, exported.series, 16)
        env.clock.advance(days=1)
        assert rig.repair_copy(good) is False
        rate(env, exported.series, 10)  # rated back down: the shelf admits it again
        assert held_repair(env).retry_held(exported.unit_id) is True
        assert check_cbz(damaged, expected_pages=1, expected_hash=None).ok
        assert not held_path(env.lib.data, exported.unit_id).exists()
        assert [e.payload["reason"] for e in rig.events("unit.upgraded")] == ["integrity_reacquire"]


def test_a_repair_with_an_open_barrier_is_applied_as_before() -> None:
    with family_env(13) as env:
        exported, _edition = served_on_family_shelf(env)
        rig = Rig(env, exported)
        good = rig.received().read_bytes()
        flip_page_byte(rig.received())
        rig.checker().check_unit(exported.unit_id)
        env.clock.advance(days=1)
        assert rig.repair_copy(good) is True
        assert not held_path(env.lib.data, exported.unit_id).exists()


def _replica_row(env, unit_id: str):  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        return connection.execute(
            sa.text(
                "SELECT id, independent, verified_at, damaged_at FROM replica "
                "WHERE unit_id = :u AND location = 'library'"
            ),
            {"u": unit_id},
        ).one()


def test_a_damaged_copy_is_never_counted_never_exported_never_a_purge_source() -> None:
    with export_env() as env:
        exported = export_one(env)
        rig = Rig(env, exported)
        with env.engine.connect() as connection:
            assert independent_copies(connection, exported.unit_id) == 1
            assert export_sources(connection, [exported.unit_id])[exported.unit_id].local_path
        flip_page_byte(rig.received())
        assert rig.checker().check_unit(exported.unit_id).state == "corrupt"
        row = _replica_row(env, exported.unit_id)
        assert row.damaged_at is not None and not row.independent
        with env.engine.connect() as connection:
            (copy,) = library_copies(connection, exported.unit_id)
            assert copy.damaged is True
            assert independent_copies(connection, exported.unit_id) == 0  # no purge on its word
            assert store_proofs(connection, exported.unit_id) == []
            source = export_sources(connection, [exported.unit_id])[exported.unit_id]
            assert source.local_path is None  # no file to read: nothing exported from it
            edition = connection.execute(
                sa.text("SELECT edition_id FROM unit WHERE id = :u"), {"u": exported.unit_id}
            ).scalar_one()
            assert exported.unit_id not in readable_unit_ids(connection, edition)


def test_a_healthy_imported_copy_is_still_exported_and_never_called_damaged() -> None:
    with export_env() as env:
        exported = export_one(env)
        with env.engine.begin() as connection:  # what an import leaves: not independent, unverified
            connection.execute(
                sa.text(
                    "UPDATE replica SET independent = 0, verified_at = NULL WHERE unit_id = :u"
                ),
                {"u": exported.unit_id},
            )
        with env.engine.connect() as connection:
            (copy,) = library_copies(connection, exported.unit_id)
            assert copy.damaged is False
            assert export_sources(connection, [exported.unit_id])[exported.unit_id].local_path
            assert independent_copies(connection, exported.unit_id) == 0
        assert _replica_row(env, exported.unit_id).damaged_at is None


def test_the_repair_clears_the_mark() -> None:
    with export_env() as env:
        exported = export_one(env)
        rig = Rig(env, exported)
        good = rig.received().read_bytes()
        flip_page_byte(rig.received())
        rig.checker().check_unit(exported.unit_id)
        env.clock.advance(days=1)
        assert rig.repair_copy(good) is True
        row = _replica_row(env, exported.unit_id)
        assert row.damaged_at is None and row.independent and row.verified_at is not None
