"""Mettre à l'abri (EF-BIB-038, EF-BIB-042 (4), EF-BIB-048) and the optional purge of the upstream
copy (EF-SRC-009, EF-EXP-064): real files in /library and /store, a real database, a stand-in for
the connected service. Every rule is played through the code that runs at night."""

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import FailureLedger
from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.schema import acquisition, identity, storage
from shelfhold.library.adapters.meta_projection import meta_path
from shelfhold.library.adapters.origin_comicinfo import origin_rel
from shelfhold.library.public import (
    PinnedCopy,
    PurgeRefused,
    StoreBudget,
    independent_copies,
    verify_kept,
)
from tests.library_env import JPEG, PNG, cbz_bytes, lib_env
from tests.locks_env import work_lock
from tests.safeguard_env import ROOMY, World

pytestmark = pytest.mark.req("EF-BIB-038", "EF-SRC-009", "EF-EXP-064", "EF-BIB-042")


class Buried:
    """A tombstone gate that covers everything."""

    def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
        return SimpleNamespace(blocked=True)

    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return True


def sweep(world: World, **kw: Any) -> Any:
    return asyncio.run(world.purge(**kw).sweep())


def plan_refusal(world: World, item: Any, **kw: Any) -> str:
    purge = world.purge(**kw)
    (candidate,) = [c for c in world.candidates() if c.unit_id == item.unit_id]
    with pytest.raises(PurgeRefused) as refused:
        purge.plan(candidate)
    return refused.value.reason


# -- the copy comes first -----------------------------------------------------------------------


def test_safeguard_copies_to_store_before_purge() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add(owed=True)
        seen: list[int] = []
        world.upstream.after_poll = lambda: seen.append(len(world.store_files()))
        first = sweep(world)  # the unit is owed a copy: nothing is erased upstream yet
        assert (first.purged, first.refused) == (0, {"safeguard_pending": 1})
        assert world.upstream.released == [] and world.store_files() == []
        report = world.run_cycle()
        assert (report.owed, report.kept, report.failed) == (1, 1, 0)
        (stored,) = world.store_files()
        assert stored.read_bytes() != b"" and item.library.exists()
        second = sweep(world)
        assert second.purged == 1 and world.upstream.released == [item.ticket]
        assert seen == [1]  # the copy was there when the service was asked
        assert world.ticket_of(item) is None
        with env.engine.connect() as c:  # the audit names the Unit by identifier, never a title
            rows = c.execute(sa.select(identity.audit.c.action, identity.audit.c.target)).all()
        assert ("unit.upstream_purged", f"unit:{item.unit_id}") in [tuple(r) for r in rows]
        assert [i.state for i in world.intents()] == ["done"]


def test_safeguard_never_hardlinks_distinct_inode() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add(owed=True)
        world.run_cycle()
        (stored,) = world.store_files()
        assert stored.stat().st_ino != item.library.stat().st_ino
        assert stored.stat().st_nlink == 1 and item.library.stat().st_nlink == 1
        assert verify_kept(env.engine, env.fsops, item.unit_id) >= 1
        with env.engine.connect() as c:
            assert independent_copies(c, item.unit_id) == 2


# -- the purge needs a copy, or a live source ---------------------------------------------------


def test_purge_refused_without_store_or_live_source() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add(owed=True)  # the origin is gone and nothing was copied
        assert plan_refusal(world, item) == "safeguard_pending"
        world.run_cycle()
        (stored,) = world.store_files()
        stored.unlink()  # the copy was lost since: the registry still names it
        assert plan_refusal(world, item) == "safeguard_pending"
        assert sweep(world).purged == 0 and world.upstream.released == []


def test_purge_allowed_with_live_source_and_no_copy_owed() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()  # nothing owes this unit a copy: the origin can give it back
        assert sweep(world).purged == 1
        assert world.upstream.released == [item.ticket] and world.store_files() == []


# -- batches ------------------------------------------------------------------------------------


def test_safeguard_budget_caps_batch_and_emits_backlog() -> None:
    with lib_env() as env:
        world = World(env)
        for _ in range(3):
            world.add(owed=True)
        report = world.run_cycle(StoreBudget(10**12, 0, max_units=2))
        assert (report.owed, report.selected, report.kept, report.backlog) == (3, 2, 2, 1)
        assert len(world.store_files()) == 2
        assert [e for e, _ in world.events] == ["unit.safeguard_backlog"]
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.safeguard_backlog)).one()
            assert (row.name, row.units) == ("store", 1) and row.bytes > 0
        world.events.clear()
        again = world.run_cycle(ROOMY)  # the next window finishes it, and the backlog empties
        assert (again.kept, again.backlog) == (1, 0) and world.events == []
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.safeguard_backlog.c.units)).scalar_one() == 0


def test_safeguard_does_not_fill_the_volume_past_the_reserve() -> None:
    with lib_env() as env:
        world = World(env)
        world.add(owed=True)
        report = world.run_cycle(StoreBudget(free_bytes=100, reserve_bytes=100))
        assert (report.selected, report.backlog) == (0, 1) and world.store_files() == []


# -- the Kavita copy ----------------------------------------------------------------------------


def test_purge_upstream_only_after_verified_export() -> None:
    with lib_env() as env:
        waiting = World(env, record_state="planned")  # Kavita was never seen to hold it
        item = waiting.add()
        assert plan_refusal(waiting, item) == "no_kavita_copy"
    with lib_env() as env:
        world = World(env)
        item = world.add()
        item.library.write_bytes(item.data + b"!")  # the exported file no longer matches its hash
        assert plan_refusal(world, item) == "no_kavita_copy"
        item.library.write_bytes(item.data)
        item.library.unlink()
        assert plan_refusal(world, item) == "no_kavita_copy"
        assert world.upstream.released == []


def test_purge_off_by_default_and_per_shelf() -> None:
    with lib_env() as env:
        world = World(env, option=False)
        world.add()
        report = sweep(world)
        assert (report.planned, report.purged) == (0, 0) and world.upstream.released == []


# -- the race: the copy changes between the plan and the deed -----------------------------------


def planned(world: World, item: Any) -> Any:
    purge = world.purge()
    (candidate,) = [c for c in world.candidates() if c.unit_id == item.unit_id]
    return purge, purge.plan(candidate)


def owed_and_kept(world: World) -> Any:
    item = world.add(owed=True)
    world.run_cycle()
    return item


def test_upstream_purge_reverifies_store_copy_at_execution() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        purge, plan = planned(world, item)
        assert plan.pinned is not None
        (stored,) = world.store_files()
        stored.unlink()  # between the plan and the deed
        with pytest.raises(PurgeRefused) as refused:
            asyncio.run(purge.execute(plan))
        assert refused.value.reason == "store_copy_missing"
        assert world.upstream.released == [] and world.ticket_of(item) == item.ticket
        assert [i.state for i in world.intents()] == ["cancelled"]


def test_upstream_purge_refuses_a_modified_store_copy() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        purge, plan = planned(world, item)
        (stored,) = world.store_files()
        raw = bytearray(stored.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        stored.write_bytes(bytes(raw))  # same inode, other bytes
        with pytest.raises(PurgeRefused) as refused:
            asyncio.run(purge.execute(plan))
        assert refused.value.reason == "store_copy_changed"
        assert world.upstream.released == []


def test_upstream_purge_refuses_a_replaced_inode() -> None:
    """The freed inode number may be handed out again (ext4, overlay): the guard does not rest on
    it, it also compares the times the disk keeps, so the test holds with or without reuse."""
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        purge, plan = planned(world, item)
        (stored,) = world.store_files()
        data = stored.read_bytes()
        stored.unlink()
        stored.write_bytes(
            data
        )  # the very same bytes under a new inode: not the copy that was proved
        with pytest.raises(PurgeRefused) as refused:
            asyncio.run(purge.execute(plan))
        assert refused.value.reason == "store_copy_changed"
        assert world.upstream.released == []


def test_upstream_purge_refuses_when_the_copy_became_a_link() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        purge, plan = planned(world, item)
        (stored,) = world.store_files()
        stored.unlink()
        try:
            os.link(item.library, stored)  # a hard link to the /library file
        except OSError:
            pytest.skip("hard links are not available here")
        with pytest.raises(PurgeRefused):
            asyncio.run(purge.execute(plan))
        assert world.upstream.released == []


def test_facts_changing_between_plan_and_deed_refuse_the_deed() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()
        purge, plan = planned(world, item)
        with env.engine.begin() as c:  # the shelf withdraws its consent in the meantime
            c.execute(sa.text("UPDATE shelf SET purge_upstream_after_export = 0"))
        with pytest.raises(PurgeRefused) as refused:
            asyncio.run(purge.execute(plan))
        assert refused.value.reason == "option_off" and world.upstream.released == []


# -- tombstones ---------------------------------------------------------------------------------


def test_tombstoned_unit_never_safeguarded() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add(owed=True)
        report = world.run_cycle(gate=Buried())
        assert (report.tombstoned, report.kept, report.selected) == (1, 0, 0)
        assert world.store_files() == [] and item.library.exists()
        assert plan_refusal(world, item, gate=Buried()) == "tombstoned"
        assert sweep(world, gate=Buried()).purged == 0 and world.upstream.released == []


# -- a paused service ---------------------------------------------------------------------------


def test_purge_never_acts_on_a_paused_source() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()
        world.pause_service()  # source_instance_changed
        assert plan_refusal(world, item) == "service_paused"
        assert sweep(world).purged == 0 and world.upstream.released == []


def test_purge_never_acts_when_roles_are_unverified() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()
        world.set_settings(roles_unverified=True)
        assert plan_refusal(world, item) == "roles_unverified"
        assert world.upstream.released == []


def test_purge_never_acts_on_a_source_not_loaded() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()
        world.set_settings()
        with env.engine.begin() as c:
            FailureLedger("suwayomi").mark_not_loaded(c, "suwayomi:1")
        assert plan_refusal(world, item) == "source_not_loaded"
        assert world.upstream.released == []


# -- crashes ------------------------------------------------------------------------------------


def test_crash_between_copy_and_purge_loses_nothing_and_purges_once() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)  # the process dies here
        again = World.__new__(World)  # a new process over the same disks and database
        again.__dict__.update(world.__dict__)
        assert sweep(again).purged == 1
        assert sweep(again).purged == 0  # nothing left to ask: no second erasure
        assert world.upstream.released == [item.ticket]
        assert len(world.store_files()) == 1


def test_crash_between_purge_and_database_never_purges_twice() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        # the service did erase, the database was never told, an intent was left applying
        world.upstream.held.discard(item.ticket)
        intent = world.journal.record(
            op="purge_upstream", expected_file_id=PinnedCopy(0, 0, 0, request_id=item.request_id)
        )
        world.journal.mark_applying(intent)
        report = sweep(world)
        assert (report.purged, report.already_gone) == (0, 1)
        assert world.upstream.released == [] and world.ticket_of(item) is None
        assert [i.state for i in world.intents()] == ["done"]
        assert len(world.store_files()) == 1  # the copy is still there: nothing lost
        assert sweep(world).planned == 0


def test_boot_never_replays_an_applying_purge_and_closes_a_pending_one() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()
        begun = world.journal.record(
            op="purge_upstream", expected_file_id=PinnedCopy(0, 0, 0, request_id=item.request_id)
        )
        world.journal.mark_applying(begun)
        never_begun = world.journal.record(op="purge_upstream")
        report = IntentJournal(env.engine, env.fsops, clock=world.clock).resolve_open()
        assert never_begun in report.abandoned and begun in report.drifted
        assert {i.id: i.state for i in world.intents()} == {
            never_begun: "cancelled",
            begun: "applying",
        }
        assert world.upstream.released == []  # the boot never reaches the service


def test_database_write_failing_after_the_deed_is_recovered_by_the_next_sweep() -> None:
    with lib_env() as env:
        world = World(env)
        item = world.add()
        purge = world.purge()
        original = purge._settle

        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("the database went away")

        purge._settle = boom  # type: ignore[method-assign]
        report = asyncio.run(purge.sweep())
        assert report.failed == 1 and world.upstream.released == [item.ticket]
        assert world.ticket_of(item) == item.ticket  # the database did not learn it
        purge._settle = original  # type: ignore[method-assign]
        again = asyncio.run(purge.sweep())
        assert (again.purged, again.already_gone) == (0, 1)
        assert world.upstream.released == [item.ticket] and world.ticket_of(item) is None


# -- the original ComicInfo ---------------------------------------------------------------------


def test_original_comicinfo_kept_for_irreplaceable_candidates() -> None:
    xml = b"<ComicInfo><Series>Dr Stone</Series><Number>1</Number></ComicInfo>"
    with lib_env() as env:
        world = World(env)
        unit_id, variant_id = env.chain["unit"], env.chain["variant"]
        from shelfhold.library.public import PassthroughStore
        from tests.null_tombstones import NONE_PURGED

        data = cbz_bytes((("001.png", PNG), ("002.jpg", JPEG)), comicinfo=xml)
        PassthroughStore(
            env.engine, env.fsops, env.locate, env.data, tombstones=NONE_PURGED,
hold_work=work_lock(env.engine),
        ).ingest(
            unit_id=unit_id, variant_id=variant_id, staged=env.stage(data),
            dest=str(env.manga / "Dr Stone" / "Dr Stone - 1.cbz"),
        )  # fmt: skip
        kept = env.data / origin_rel(unit_id)
        assert kept.read_bytes() == xml  # the exporter will replace the one in the file
        unit_json = meta_path(env.data, unit_id).read_text()
        assert "origin_comicinfo" in unit_json and origin_rel(unit_id) in unit_json
        assert world is not None


# -- the failure matrix -------------------------------------------------------------------------


def test_one_event_per_run_for_failures() -> None:
    with lib_env() as env:
        world = World(env)
        for _ in range(2):
            item = world.add(owed=True)
            item.library.write_bytes(b"not an archive")  # the copy cannot be made
        report = world.run_cycle()
        assert report.failed == 2 and report.kept == 0
        assert [e for e, _ in world.events] == ["unit.safeguard_failed"]
        assert world.store_files() == []
        with env.engine.connect() as c:
            tickets = c.execute(sa.select(acquisition.download_request.c.ticket)).scalars().all()
        assert len(tickets) == 2 and all(tickets)  # nothing upstream was touched
