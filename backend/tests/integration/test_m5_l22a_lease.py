"""M5 L22a (EF-LEC-014, debt of M4 L35 #339 point 3): the progress sync takes the Work's lease.

An upgrade, a team switch or an assembly holds `work:<id>` for the whole of the replacement, and
its guard "chapter read" is looked at under that lease. A "read" that lands between the guard and
the swap (the guard and the swap are not one transaction: the swap is on the disk) used to be
recorded for a chapter the same job then replaced. Every writer of a position (the reader, the
e-reader's kosync, the pass pulling Kavita's state) now takes the same lease before it writes, and
waits for it: a lease that stays held answers "busy", writes nothing, and the device or the next
pass tries again. The doors are gated, no sleep: the holder is the test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.locks import ResourceLocks, work_resource
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import reading
from shelfhold.progress_lease import build_progress_lease
from shelfhold.reading.application.lease import ProgressBusyError, progress_leased
from shelfhold.reading.application.sync import sync_member
from shelfhold.reading.public import Kosync
from tests.integration.test_m4_l35_team_switch_contradicteur import _queued
from tests.integration.test_m5_l07_positions import World, count, kept, page, put, stamp
from tests.integration.test_m5_l09_kavita_epub import DIGEST, epub_chapter
from tests.sync_env import SyncEnv, sync_env

pytestmark = pytest.mark.req("EF-LEC-014")


class Gate:
    """A lease the test holds or lets go: what `hold_work` is, with no lock table behind it."""

    def __init__(self) -> None:
        self.held = False
        self.asked: list[str] = []

    def __call__(self, work_id: str) -> AbstractContextManager[None]:
        self.asked.append(work_id)

        @contextmanager
        def lease() -> Iterator[None]:
            if self.held:
                raise ProgressBusyError
            yield

        return lease()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The lease of the real application, with no wait (a test never sleeps); the waits asked."""
    waits: list[float] = []

    def build(engine: sa.Engine, clock: Any, timeout: float = 5.0) -> Callable[..., Any]:
        waits.append(timeout)
        return build_progress_lease(engine, clock, timeout=0.0)

    monkeypatch.setattr("shelfhold.main.build_progress_lease", build)
    return waits


@pytest.fixture
def world(
    spy: list[float], monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> Iterator[World]:
    from tests.integration.test_l05a_content_route import _world
    from tests.integration.test_m5_l06_publication import ServerCutArchives

    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def _holding(world: World, work_id: str) -> AbstractContextManager[Any]:
    """What an upgrade or a team switch does: the lock of the Work, for the whole replacement."""
    locks = ResourceLocks(world.seed.engine, clock=world.env.clock)
    return locks.hold_resources(new_ulid(), [work_resource(work_id)])


def test_progress_sync_waits_for_intent_lease(world: World, spy: list[float]) -> None:
    """The reader's own door: while a job holds the Work, the position is not written and the
    answer is `progress_busy` (503, retry); once the job lets go, the same request is stored."""
    content, _unit = kept(world)
    work = world.seed.works["ok"]
    with _holding(world, work):
        busy = put(world, world.admin, content, page(5))
        assert busy.status_code == 503, busy.text
        assert busy.json()["code"] == "progress_busy"
        assert count(world, reading.reading_position) == 0
    assert put(world, world.admin, content, page(6), minutes=1).status_code == 200
    assert count(world, reading.reading_position) == 1
    assert spy  # the application asked for its lease through the builder


def test_the_lease_of_one_work_never_holds_back_another(world: World) -> None:
    content, _unit = kept(world, edition="ok")
    other_work = world.seed.works["twelve"]
    with _holding(world, other_work):
        assert put(world, world.admin, content, page(5)).status_code == 200


def test_a_hidden_work_is_not_found_whether_a_job_holds_it_or_not(world: World) -> None:
    """The attack: a child asks to write on a Work above her ceiling while an upgrade holds it. A
    `progress_busy` would say that the Work exists and is being rewritten; the answer is the one of
    a carrier that does not exist, byte for byte, with the lease held or not."""
    hidden, _unit = kept(world, edition="twelve")
    free = put(world, world.kid, hidden, page(1))
    with _holding(world, world.seed.works["twelve"]):
        held = put(world, world.kid, hidden, page(1))
    assert free.status_code == held.status_code == 404
    assert (free.json()["code"], len(free.content)) == (held.json()["code"], len(held.content))
    assert count(world, reading.reading_position) == 0


def test_a_key_or_a_device_token_is_refused_the_same_way_with_the_lease_held(
    world: World,
) -> None:
    """The attack: a key of the API and a device token never write a position of the reader, and
    holding the Work must not turn their refusal into a `progress_busy` (it would tell a key that
    a job is rewriting the Work). The session is the only credential that gets as far as the lease."""
    import base64

    from shelfhold.identity.public import local_auth

    content, _unit = kept(world)
    auth = local_auth(world.seed.engine, world.env.settings)
    key = auth.create_api_key(name="reader", username="admin", read_only=False).value
    created = auth.create_device_token(subject_username="mum", name="liseuse")
    raw = base64.b64encode(f"{created.public_id}:{created.secret}".encode()).decode()
    body = {"carrier_type": "content", "carrier_id": content, "locator": page(1),
            "client_ts": stamp(world)}  # fmt: skip
    with _holding(world, world.seed.works["ok"]):
        for ip, header in (("10.0.0.9", f"Bearer {key}"), ("10.0.0.8", f"Basic {raw}")):
            got = world.env.other_client(ip).put(
                "/api/reader/positions", json=body, headers={"Authorization": header}
            )
            assert got.status_code in {401, 403}, got.text
    assert count(world, reading.reading_position) == 0


def _epub_service(env: SyncEnv, monkeypatch: pytest.MonkeyPatch, gate: Gate) -> Kosync:
    monkeypatch.setattr(Kosync, "_kind", lambda _self, _c, _item: "reflowable")
    return Kosync(env.engine, lambda: None, env.clock.now, hold_work=gate)  # type: ignore[arg-type, return-value]


def test_kosync_waits_for_the_lease_and_writes_nothing_while_it_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = epub_chapter(env)
        gate = Gate()
        service = _epub_service(env, monkeypatch, gate)
        say = {
            "user_id": reader, "token": "kobo-token", "document": DIGEST,
            "progress": "/body/DocFragment[7]/body/div[2]/p[3]/text().11",
            "percentage": 0.5, "device": "Libra 2", "device_id": "A1B2C3",
        }  # fmt: skip
        gate.held = True
        with pytest.raises(ProgressBusyError):
            service.push(**say)
        assert env.positions(reader) == []
        gate.held = False
        assert service.push(**say) is True
        assert len(env.positions(reader)) == 1
        with env.engine.connect() as connection:
            work = connection.execute(
                sa.text(
                    "SELECT e.work_id FROM edition AS e JOIN export_item AS i"
                    " ON i.edition_id = e.id WHERE i.id = :c"
                ),
                {"c": chapter.carrier},
            ).scalar_one()
        assert set(gate.asked) == {work}  # the Work of the file, whatever the digest said


def test_the_pull_from_kavita_leaves_a_busy_chapter_for_the_next_pass(tmp_path: Path) -> None:
    """A pass that finds the Work held does not write what Kavita says, and does not note it as
    seen either (the next pass would find it unchanged and never take it)."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 7)
        env.clock.advance(minutes=1)
        gate = Gate()
        gate.held = True

        def pass_once() -> Any:
            return sync_member(
                env.engine, env.remote, user_id=reader, destination_id=env.destination,
                clock=env.clock, hold_work=gate,
            )  # fmt: skip

        first = pass_once()
        assert (first.code, first.pulled, first.busy) == ("ok", 0, 1)
        assert env.positions(reader, source="server") == []
        gate.held = False
        env.clock.advance(minutes=1)
        second = pass_once()
        assert (second.pulled, second.busy) == (1, 0)
        assert [p.locator["page"] for p in env.positions(reader, source="server")] == [7]


def test_a_read_cannot_land_between_the_guard_and_the_swap_of_an_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window of M4 L35: at the instant the upgrade places its new file under the intent (the
    guard "read" is behind it, the swap is not done), a writer of a position is refused; before
    and after, it is not. The lease is the real one, the lock the upgrade really takes."""
    import shelfhold.export.application.upgrade as upgrade_module
    from tests.export_env import export_env
    from tests.team_switch_env import RealUpgrader, team_switch

    from shelfhold.acquisition.application.team_switch import TeamSwitchService

    with export_env() as env:
        s = team_switch(env)
        lease = build_progress_lease(env.engine, env.clock, timeout=0.0)
        outcomes: list[str] = []

        def attempt() -> str:
            try:
                with progress_leased(env.engine, lease, "export_item", s.exported.export_item_id):
                    return "written"
            except ProgressBusyError:
                return "busy"

        outcomes.append(attempt())  # before the job: the lease is free
        real = upgrade_module.place_under_intent

        def spying(*args: Any, **kwargs: Any) -> Any:
            outcomes.append(attempt())  # the guard has been read, the swap is not done
            return real(*args, **kwargs)

        monkeypatch.setattr(upgrade_module, "place_under_intent", spying)
        service = TeamSwitchService(env.engine, env.clock, submit=lambda *_a: "01JJOB")
        switch_id = _queued(service, s.edition_id)
        service.run(switch_id, upgrader=RealUpgrader(env))
        outcomes.append(attempt())  # after: the job has let go

        assert outcomes == ["written", "busy", "written"]
