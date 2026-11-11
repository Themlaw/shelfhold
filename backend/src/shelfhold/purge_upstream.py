"""The connected service, as `purge --now` and the M1b migration reach it (EF-SRC-033, EF-SRC-029).

`ConnectedUpstream` is the adapter of the purge's `UpstreamPort`. The download of a Unit goes by
the deed of "Mettre à l'abri" (`UpstreamPurge.release_for_purge`: the intent that names the
request, the service asked before and after, one erasure at most per intent), never by a call of
its own. A series leaves the library through `FollowRequests.unfollow` (its pause check, its
fingerprint, its idempotence). What the service answers is turned into codes: no message of the
service, which may carry a title, goes into a report, a log or the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, contextmanager, nullcontext, suppress
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import Engine

from shelfhold.acquisition.public import FollowRequests
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.upstream_port import (
    SeriesKey,
    UnfollowResult,
    UpstreamResult,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.wake import WakeDeferred
from shelfhold.library.public import (
    DownloadTicket,
    PurgeRefused,
    PurgeUnresolvedError,
    ServiceGate,
    UpstreamPurge,
)
from shelfhold.sdk.connected import Ticket
from shelfhold.sdk.refs import SeriesRef

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["ConnectedUpstream", "connected_upstream"]

log = structlog.get_logger(__name__)
PLUGIN_ID = "suwayomi"
CONTENT_REMOVED = "source.content_removed"  # `sdk.source.ContentRemoved.code`


class _Unscreened:
    """`UpstreamPurge` asks a tombstone screen of the deeds of the window job; the deed of the
    purge is made under the tombstone it has just written and never asks."""

    def check(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover (not asked)
        raise AssertionError("the deed of the purge does not read the tombstones")

    check_content = check


class ConnectedUpstream:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        journal: IntentJournal,
        service_gate: ServiceGate,
        service: Any,
        fetcher: Any,
        clock: Clock,
        *,
        unfollow: Callable[[str, str, str], Awaitable[None]],
        awake: Callable[[], AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self._service = service
        self._fetcher = fetcher
        self._unfollow = unfollow
        self._awake = awake
        self._purge = UpstreamPurge(
            engine, fsops, journal, _Unscreened(), service_gate, service, fetcher, clock,
            ticket=lambda token: Ticket(token=token, target_kind="unit"),
        )  # fmt: skip
        self._runner: asyncio.Runner | None = None

    def close(self) -> None:
        if self._runner is not None:
            self._runner.close()
            self._runner = None

    def _run(self, work: Awaitable[Any]) -> Any:
        """One loop for the whole purge (the transport of the service is shared by the calls)."""
        if self._runner is None:
            self._runner = asyncio.Runner()

        async def go() -> Any:
            async with self._awake() if self._awake is not None else nullcontext():
                return await work

        return self._runner.run(go())

    # -- the port --------------------------------------------------------------------------------

    def release(self, tickets: Sequence[DownloadTicket]) -> list[UpstreamResult]:
        if not tickets:
            return []
        out: list[UpstreamResult] = []

        async def each() -> None:
            for target in tickets:
                out.append(await self._release_one(target))

        try:
            self._run(each())
        except WakeDeferred:  # the service sleeps and was not woken: nothing was sent
            done = {r.request_id for r in out}
            out.extend(
                UpstreamResult(t.request_id, "unavailable", "wake_deferred")
                for t in tickets
                if t.request_id not in done
            )
        return out

    async def _release_one(self, target: DownloadTicket) -> UpstreamResult:
        try:
            done = await self._purge.release_for_purge(target)
        except PurgeRefused as refused:
            return UpstreamResult(target.request_id, "refused", refused.reason)
        except PurgeUnresolvedError:
            return UpstreamResult(target.request_id, "unresolved")
        except WakeDeferred:
            raise
        except Exception as error:  # the service did not answer: asked again next time
            log.warning("purge.upstream_unavailable", error_type=type(error).__name__)
            return UpstreamResult(target.request_id, "unavailable", "service_error")
        return UpstreamResult(target.request_id, done.kind, freed_bytes=done.freed_bytes)

    def unfollow(self, series: Sequence[SeriesKey]) -> list[UnfollowResult]:
        if not series:
            return []
        out: list[UnfollowResult] = []

        async def each() -> None:
            for key in series:
                out.append(await self._unfollow_one(key))

        try:
            self._run(each())
        except WakeDeferred:
            done = {r.key for r in out}
            out.extend(UnfollowResult(k, False, "wake_deferred") for k in series if k not in done)
        return out

    async def _unfollow_one(self, key: SeriesKey) -> UnfollowResult:
        plugin_id, source_key, remote_id = key
        if plugin_id != PLUGIN_ID:  # a source that is no connected service has nothing to leave
            return UnfollowResult(key, True, "no_connected_service")
        try:
            await self._unfollow(plugin_id, source_key, remote_id)
        except WakeDeferred:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if code == CONTENT_REMOVED:  # the service no longer knows the series: it is out
                return UnfollowResult(key, True, "already_gone")
            log.warning("purge.unfollow_failed", error_type=type(error).__name__)
            return UnfollowResult(key, False, code if isinstance(code, str) else "service_error")
        return UnfollowResult(key, True)

    def holds(self, tickets: Sequence[DownloadTicket]) -> dict[str, bool]:
        found: dict[str, bool] = {}

        async def each() -> None:
            for target in tickets:
                try:
                    state = await self._service.poll(
                        self._fetcher, Ticket(token=target.ticket, target_kind="unit")
                    )
                except WakeDeferred:
                    raise
                except Exception as error:
                    log.warning("purge.probe_failed", error_type=type(error).__name__)
                    continue
                found[target.request_id] = str(state) == "ready"

        with suppress(WakeDeferred):
            self._run(each())
        return found

    def followed(self) -> set[tuple[str, str]] | None:
        async def ask() -> set[tuple[str, str]]:
            listed = await self._service.followed_series(self._fetcher)
            return {(r.source_key, r.remote_id) for r in listed}

        try:
            return self._run(ask())  # type: ignore[no-any-return]
        except WakeDeferred:
            return None
        except Exception as error:
            log.warning("purge.followed_failed", error_type=type(error).__name__)
            return None


@contextmanager
def connected_upstream(
    comp: Composition, fsops: Fsops, journal: IntentJournal
) -> Iterator[ConnectedUpstream | None]:
    """The adapter over the configured Suwayomi, None when there is none (M1a: the claim is
    withdrawn on our side and the admin is told to delete in Suwayomi)."""
    from shelfhold.safeguard_jobs import service_refusal

    link = comp.suwayomi()
    if link is None:
        yield None
        return
    requests = FollowRequests(comp.engine, comp.clock, lambda: link)

    async def unfollow(plugin_id: str, source_key: str, remote_id: str) -> None:
        ref = SeriesRef(source_key=source_key, remote_id=remote_id, title="")
        await requests.unfollow(ref, remove_upstream=True, confirmed=True)

    def awake() -> AbstractAsyncContextManager[None]:
        target = getattr(link.service, "wake_target", None)
        return comp.wakes().session(target) if target else nullcontext()

    adapter = ConnectedUpstream(
        comp.engine, fsops, journal, service_refusal, link.service, link.fetcher, comp.clock,
        unfollow=unfollow, awake=awake,
    )  # fmt: skip
    try:
        yield adapter
    finally:
        adapter.close()
        asyncio.run(link.guarded.aclose())
