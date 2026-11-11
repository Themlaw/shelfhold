"""EF-SRC-033, EF-BIB-052: what the adapter of the purge turns the service's answers into (L17).

The real `ConnectedUpstream` over the real `UpstreamPurge` and the real database; the service is a
fake only at the network edge, and answers each way a service can: down, asleep, refusing, not
holding the chapter, or not knowing the series."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.upstream_port import (
    ALREADY_GONE,
    CANCELLED,
    RELEASED,
)
from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.wake import WakeDeferred
from shelfhold.library.public import download_tickets
from shelfhold.purge_upstream import ConnectedUpstream
from shelfhold.safeguard_jobs import service_refusal
from tests.export_env import ExportEnv, export_env
from tests.purge_env import TITLE, PurgeEnv, make_purge_env, spread
from tests.purge_m1b_env import FakeService, fetched_ticket

pytestmark = pytest.mark.req("EF-BIB-052", "EF-SRC-033")


class _Broken(FakeService):
    async def poll(self, f: Any, t: Any) -> str:
        raise ConnectionResetError("the service did not answer")


def _adapter(
    env: ExportEnv,
    base: PurgeEnv,
    service: FakeService,
    gate: Any = service_refusal,
    **options: Any,
) -> ConnectedUpstream:
    from shelfhold.export.application.intents import IntentJournal

    async def unfollow(plugin_id: str, source_key: str, remote_id: str) -> None:
        await service.unfollow(plugin_id, source_key, remote_id)

    return ConnectedUpstream(
        env.engine, env.lib.fsops, IntentJournal(env.engine, env.lib.fsops, clock=base.clock),
        gate, service, object(), base.clock, unfollow=unfollow, **options,
    )  # fmt: skip


def _tickets(env: ExportEnv, unit: str) -> Any:
    with env.engine.connect() as connection:
        return download_tickets(connection, [unit])


def test_a_service_that_does_not_answer_is_asked_again_next_time(tmp_path: Path) -> None:
    with export_env() as env:
        service = _Broken(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "t-1")
        upstream = _adapter(env, make_purge_env(env.lib), service)
        [result] = upstream.release(_tickets(env, unit))
        assert (result.outcome, result.reason) == ("unavailable", "service_error")
        assert service.released() == []
        assert upstream.holds(_tickets(env, unit)) == {}  # no answer: left out, never guessed
        with env.engine.connect() as connection:  # the ticket is still there for the next run
            assert connection.execute(sa.select(acquisition.download_request.c.ticket)).scalar()
        upstream.close()


def test_a_paused_service_is_not_given_a_delete(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "t-1")
        upstream = _adapter(
            env, make_purge_env(env.lib), service, gate=lambda *_: "service_paused"
        )  # the probe found another installation
        [result] = upstream.release(_tickets(env, unit))
        assert (result.outcome, result.reason) == ("refused", "service_paused")
        assert service.released() == []
        upstream.close()


def test_a_chapter_the_service_no_longer_holds_is_settled_without_a_delete(
    tmp_path: Path,
) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "t-1")
        (tmp_path / "held.json").write_text("[]")  # the admin deleted it in the service
        upstream = _adapter(env, make_purge_env(env.lib), service)
        [result] = upstream.release(_tickets(env, unit))
        assert result.outcome in (ALREADY_GONE, CANCELLED) and service.released() == []
        assert upstream.release(_tickets(env, unit)) == []  # the ticket was let go of
        upstream.close()


def test_a_queued_chapter_is_taken_out_of_its_queue_then_released_once(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "t-1", state="requested")
        upstream = _adapter(env, make_purge_env(env.lib), service)
        [result] = upstream.release(_tickets(env, unit))
        assert result.outcome == RELEASED and service.cancelled() == ["t-1"]
        assert service.released() == ["t-1"]
        upstream.close()


def test_a_service_asleep_and_not_woken_defers_the_whole_batch(tmp_path: Path) -> None:
    @asynccontextmanager
    async def asleep() -> AsyncIterator[None]:
        raise WakeDeferred("suwayomi")
        yield

    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        other, _ = spread(env, env.new_series("Another"), "9")
        fetched_ticket(env, service, unit, "t-1")
        fetched_ticket(env, service, other, "t-2")
        upstream = _adapter(env, make_purge_env(env.lib), service, awake=asleep)
        results = upstream.release([*_tickets(env, unit), *_tickets(env, other)])
        assert [(r.outcome, r.reason) for r in results] == [("unavailable", "wake_deferred")] * 2
        assert service.released() == []  # nothing was sent
        [deferred] = upstream.unfollow([("suwayomi", "k", "r")])
        assert (deferred.done, deferred.reason) == (False, "wake_deferred")
        assert upstream.followed() is None
        upstream.close()


def test_unfollow_answers_in_codes_never_in_the_services_words(tmp_path: Path) -> None:
    class Refuses(FakeService):
        async def unfollow(self, plugin_id: str, source_key: str, remote_id: str) -> None:
            error = RuntimeError(f"cannot leave {remote_id}")  # a message that spells a title
            error.code = "follow.service_paused"  # type: ignore[attr-defined]
            raise error

    with export_env() as env:
        upstream = _adapter(env, make_purge_env(env.lib), Refuses(tmp_path))
        results = upstream.unfollow([("suwayomi", "k", "/manga/dr-stone"), ("other", "k", "r")])
        assert [(r.done, r.reason) for r in results] == [
            (False, "follow.service_paused"),
            (
                True,
                "no_connected_service",
            ),  # a source that is no connected service: nothing to leave
        ]
        assert not any("dr-stone" in r.reason for r in results)  # a code, not the message
        assert upstream.unfollow([]) == [] and upstream.release([]) == []
        upstream.close()


def test_followed_lists_the_library_or_says_it_cannot(tmp_path: Path) -> None:
    class Lists(FakeService):
        async def followed_series(self, f: Any) -> list[Any]:
            from types import SimpleNamespace

            return [SimpleNamespace(source_key="suwayomi:1", remote_id="/m/a")]

    with export_env() as env:
        base = make_purge_env(env.lib)
        listing = _adapter(env, base, Lists(tmp_path))
        assert listing.followed() == {("suwayomi:1", "/m/a")}
        listing.close()
        failing = _adapter(env, base, FakeService(tmp_path))  # no `followed_series`: it fails
        assert failing.followed() is None
        failing.close()
