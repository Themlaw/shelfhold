"""Regressions of the adversarial review of L16 (PR #118): three ways to lose data, each proved
on real disks and a real database. The upstream erasure is irreversible, so each one is a refusal
or a hand-over, never a second try."""

import asyncio
import json
import os
import signal
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, export, storage
from shelfhold.library.public import PassthroughStore
from shelfhold.sdk.connected import ReleaseResult
from tests.integration.test_schema_catalog import add_variant
from tests.library_env import JPEG, PNG, cbz_bytes, lib_env
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED
from tests.safeguard_env import World

pytestmark = pytest.mark.req("EF-BIB-038", "EF-EXP-064", "EF-SRC-009")


def sweep(world: World) -> Any:
    return asyncio.run(world.purge().sweep())


def owed_and_kept(world: World) -> Any:
    item = world.add(owed=True)
    world.run_cycle()
    return item


# -- 1. a /store copy that is another name of a /library file -----------------------------------


def test_a_hard_link_between_library_and_store_is_not_an_independent_copy() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        (stored,) = world.store_files()
        tmp = item.library.with_suffix(".tmp")
        os.link(stored, tmp)
        os.replace(tmp, item.library)  # a dedupe tool, behind the registry's back
        assert os.stat(stored).st_ino == os.stat(item.library).st_ino
        report = sweep(world)
        assert world.upstream.released == [] and report.purged == 0
        assert report.refused == {"safeguard_pending": 1}  # the unit is still owed a real copy
        assert world.ticket_of(item) == item.ticket


def test_a_link_made_between_the_plan_and_the_deed_refuses_the_deed() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        purge = world.purge()
        (candidate,) = world.candidates()
        plan = purge.plan(candidate)
        (stored,) = world.store_files()
        tmp = item.library.with_suffix(".tmp")
        os.link(stored, tmp)
        os.replace(tmp, item.library)
        with pytest.raises(Exception) as refused:
            asyncio.run(purge.execute(plan))
        assert getattr(refused.value, "reason", "") == "store_copy_changed"
        assert world.upstream.released == []


# -- 2. a Unit with two contents ----------------------------------------------------------------


def second_content(world: World, first: Any) -> tuple[Path, bytes]:
    env = world.env
    with env.engine.begin() as c:
        variant_b = add_variant(c, first.unit_id, env.chain["binding"], "ch-1-teamB")
    data_b = cbz_bytes((("001.png", PNG + b"teamB"), ("002.jpg", JPEG + b"B")))
    dest_b = env.manga / "Dr Stone" / "Dr Stone - 1 (B).cbz"
    PassthroughStore(
        env.engine,
        env.fsops,
        env.locate,
        env.data,
        tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=first.unit_id, variant_id=variant_b, staged=env.stage(data_b), dest=str(dest_b)
    )
    with env.engine.begin() as c:
        content_b = c.execute(
            sa.select(storage.content.c.id).where(storage.content.c.variant_id == variant_b)
        ).scalar_one()
        c.execute(
            sa.insert(acquisition.download_request).values(
                id=new_ulid(), target_kind="unit", variant_id=variant_b,
                source_binding_id=env.chain["binding"], ticket="ticket-B", state="fetched",
            )
        )  # fmt: skip
        item_id = new_ulid()
        c.execute(
            sa.insert(export.export_item).values(
                id=item_id, edition_id=env.chain["edition"], kind="unit", content_id=content_b
            )
        )
        c.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=world.destination_id, export_item_id=item_id,
                locator_type="none", state="scanned",
            )
        )  # fmt: skip
    world.upstream.held.add("ticket-B")
    return dest_b, data_b


def test_a_copy_of_another_content_of_the_unit_does_not_license_the_purge() -> None:
    with lib_env() as env:
        world = World(env)
        first = world.add(owed=True, fetched=False, exported=False)
        dest_b, _ = second_content(world, first)
        world.run_cycle()  # keeps one content of the Unit
        dest_b.unlink()  # content B: no /library file, no /store copy
        report = sweep(world)
        assert world.upstream.released == [] and report.purged == 0


def test_the_kavita_copy_must_be_the_file_of_this_content() -> None:
    with lib_env() as env:
        world = World(env)
        first = world.add(fetched=False)  # content A, exported and fine; the source is alive
        dest_b, _ = second_content(world, first)
        dest_b.unlink()  # Kavita lost content B; content A's file is still there
        report = sweep(world)
        assert world.upstream.released == [] and report.refused == {"no_kavita_copy": 1}


# -- 3. a deed that was begun is never sent again -----------------------------------------------


class Flaky:
    """The service, in `ui_login`: the chapter stays downloaded for another account, so a second
    `release` would take the first erasure's claim for the other account's."""

    wake_target = None

    def __init__(self, upstream: Any) -> None:
        self.up, self.calls, self.fail = upstream, [], True

    async def poll(self, f: Any, t: Any) -> str:
        return "ready"

    async def release(self, f: Any, t: Any) -> Any:
        self.calls.append(t.token)
        if self.fail:
            raise ConnectionResetError("the answer never came")
        return ReleaseResult(released=True, freed_bytes=0, still_present=True)


def test_a_release_that_was_sent_is_never_sent_again_after_a_lost_answer() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        world.upstream = Flaky(world.upstream)  # type: ignore[assignment]
        first = sweep(world)
        assert first.failed == 1 and world.upstream.calls == [item.ticket]
        assert [i.state for i in world.intents()] == ["applying"]
        again = sweep(world)  # ambiguous (not sent, or another account keeps the file)
        assert again.handed_to_admin == 1 and again.purged == 0
        assert world.ticket_of(item) is None  # the claim goes with the intent: nothing asks again
        assert [i.state for i in world.intents()] == ["done"]
        for _ in range(2):
            assert sweep(world).handed_to_admin == 0
        assert world.upstream.calls == [item.ticket]  # one DELETE, ever


def test_a_release_that_landed_is_settled_by_asking_even_without_the_store_copy() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)

        async def erased_then_lost(f: Any, t: Any) -> Any:
            world.upstream.held.discard(t.token)
            raise ConnectionResetError("killed after the service answered")

        world.upstream.release = erased_then_lost  # type: ignore[method-assign]
        assert sweep(world).failed == 1
        for p in world.store_files():
            p.unlink()  # the copy is lost meanwhile
        report = sweep(world)
        assert (report.already_gone, report.handed_to_admin) == (1, 0)
        assert world.ticket_of(item) is None
        assert [i.state for i in world.intents()] == ["done"]
        with env.engine.connect() as c:
            actions = c.execute(sa.text("SELECT action FROM audit")).scalars().all()
        assert "unit.upstream_purged" in actions


def test_sigkill_in_the_release_then_a_resume_sends_one_delete() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        log = env.tmp / "calls.json"
        log.write_text("[]")

        class Shared:
            wake_target = None

            async def poll(self, f: Any, t: Any) -> str:
                return "ready"

            async def release(self, f: Any, t: Any) -> Any:
                calls = json.loads(log.read_text())
                log.write_text(json.dumps([*calls, t.token]))
                os.sync()
                os.kill(os.getpid(), signal.SIGKILL)  # the answer never comes
                return ReleaseResult(released=True)

        pid = os.fork()
        if pid == 0:
            world.upstream = Shared()  # type: ignore[assignment]
            asyncio.run(world.purge().sweep())
            os._exit(0)
        os.waitpid(pid, 0)
        env.engine.dispose()
        boot = IntentJournal(env.engine, env.fsops, clock=world.clock).resolve_open()
        assert len(boot.drifted) == 1  # the boot leaves it for the sweep and the admin
        world.upstream = Shared()  # type: ignore[assignment]
        report = asyncio.run(world.purge().sweep())
        assert report.handed_to_admin == 1
        assert json.loads(log.read_text()) == [item.ticket]  # one DELETE_DOWNLOADED_CHAPTER


# -- 4. the facts are read under the write lock -------------------------------------------------


def test_the_consent_is_read_and_the_deed_marked_in_one_locked_transaction() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        real = world.journal.mark_applying
        outcome: dict[str, str] = {}

        def mark(intent_id: str, *, within: Any = None) -> None:
            # a concurrent `shelf purge-upstream --off` arrives now: it must wait for the lock
            other = sqlite3.connect(env.engine.url.database, timeout=0.2)
            try:
                other.execute("UPDATE shelf SET purge_upstream_after_export = 0")
                other.commit()
                outcome["off"] = "landed"
            except sqlite3.OperationalError:
                outcome["off"] = "blocked"
            finally:
                other.close()
            real(intent_id, within=within)

        world.journal.mark_applying = mark  # type: ignore[method-assign]
        report = sweep(world)
        assert outcome == {"off": "blocked"}  # nothing can slip between the look and the mark
        assert report.purged == 1 and world.upstream.released == [item.ticket]


def test_a_consent_withdrawn_before_the_lock_stops_the_deed() -> None:
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)

        def withdraw() -> None:
            with env.engine.begin() as c:
                c.execute(sa.text("UPDATE shelf SET purge_upstream_after_export = 0"))

        world.upstream.after_poll = withdraw
        purge = world.purge()
        (candidate,) = world.candidates()
        plan = purge.plan(candidate)
        with pytest.raises(Exception) as refused:
            asyncio.run(purge.execute(plan))
        assert getattr(refused.value, "reason", "") == "option_off"
        assert world.upstream.released == [] and world.ticket_of(item) == item.ticket


def test_a_reused_inode_number_with_the_same_bytes_is_still_another_file() -> None:
    """What a runner may do (ext4, overlay): the inode number comes back with the same bytes.
    Rewriting in place gives exactly that: same `(dev, ino)`, same hashes, new times."""
    with lib_env() as env:
        world = World(env)
        item = owed_and_kept(world)
        purge = world.purge()
        (candidate,) = world.candidates()
        plan = purge.plan(candidate)
        (stored,) = world.store_files()
        before = os.stat(stored).st_ino
        data = stored.read_bytes()
        os.utime(stored, ns=(1, 1))  # an older, other file with the number and the bytes
        stored.write_bytes(data)
        assert os.stat(stored).st_ino == before
        with pytest.raises(Exception) as refused:
            asyncio.run(purge.execute(plan))
        assert getattr(refused.value, "reason", "") == "store_copy_changed"
        assert world.upstream.released == [] and world.ticket_of(item) == item.ticket
