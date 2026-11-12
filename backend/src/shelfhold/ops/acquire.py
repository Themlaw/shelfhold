"""`source search`, `source filters`, `acquire` (EF-SRC-015, EF-SRC-004): one service for the host
CLI and the routes.

Searching and resolving a URL only read. Acquiring prepares the request (Work, Edition, binding,
Unit, Variant, `download_request`) and queues one job of the `request` class (the requests of
EF-INT-017): it runs before the catch-up batches and after nothing else asks for a place, like
every other request of a person. The file itself is downloaded by that job. Not reserved actions:
they ask for a file, they delete nothing. Every refusal is a problem of the catalog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.acquisition.public import (
    ACQUIRE_JOB_KIND,
    AcquireRefused,
    NativeAcquirer,
    acquire_dedupe_key,
)
from shelfhold.infrastructure.fetcher import FetchError
from shelfhold.infrastructure.schema import acquisition
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.ops.tombstone_screens import CatalogScreens
from shelfhold.plugin_host.errors import NoSourceForUrl, RefHasSecret
from shelfhold.plugin_host.settings import SourceSettings
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import SourceError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["AcquireOps"]

REQUEST_CLASS = "request"  # EF-INT-017: a person's request, ahead of the catch-up batches


class AcquireOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    # -- the acquirer -------------------------------------------------------------------------

    def acquirer(self, *, connected_key: str | None = None) -> NativeAcquirer:
        comp = self._comp
        registry = comp.plugins()
        if registry is None:
            raise ExitUnavailableError("the sources are not loaded: the server is not running")
        link = comp.suwayomi() if connected_key == "suwayomi" else None  # opened outside the loop
        return NativeAcquirer(
            comp.engine, comp.clock, comp.bus, registry, comp.source_fetchers(),
            staging_dir=comp.settings.data_dir / "tmp",
            max_bytes=comp.settings.received_cbz_max_bytes,
            source_settings=self._settings_of,
            source_secrets=self._secrets_of,
            connected=(lambda key: link if key == "suwayomi" else None),
            work_screen=CatalogScreens(comp).open,
        )  # fmt: skip

    def _settings_of(self, plugin_id: str) -> dict[str, Any]:
        table = acquisition.source_config
        with self._comp.engine.connect() as connection:
            found = connection.execute(
                sa.select(table.c.settings).where(table.c.plugin_id == plugin_id)
            ).scalar_one_or_none()
        return dict(found or {})

    def _secrets_of(self, plugin_id: str) -> dict[str, str]:
        """The sealed secrets of a source (L07), opened for the host's own injection only."""
        comp = self._comp
        loaded = SourceSettings(comp.engine, comp.vault, now=comp.clock.now).load(plugin_id)
        return dict(loaded.values)

    @staticmethod
    def _refused(error: Exception) -> CodedRefusal:
        return CodedRefusal(str(getattr(error, "code", "fetch.error")), type(error).__name__)

    def _run(self, work: Any) -> Any:
        try:
            return self._comp.run(work)
        except (NoSourceForUrl, AcquireRefused, SourceError, RefHasSecret, FetchError) as error:
            raise self._refused(error) from None

    # -- the doors ----------------------------------------------------------------------------

    def filters(self, source_key: str) -> list[dict[str, Any]]:
        try:
            specs = self.acquirer(connected_key=source_key.partition(":")[0]).filters(source_key)
        except AcquireRefused as error:
            raise self._refused(error) from None
        return [s.model_dump(mode="json") for s in specs]

    def search(
        self, source_key: str, query: str, *, filters: dict[str, Any] | None, cursor: str | None
    ) -> dict[str, Any]:
        acquirer = self.acquirer(connected_key=source_key.partition(":")[0])
        page = self._run(acquirer.search(source_key, query, filters=filters, cursor=cursor))
        return {
            "items": [
                {"source_key": r.source_key, "remote_id": r.remote_id, "title": r.title}
                for r in page.items
            ],
            "next": page.next.token if page.next else None,
        }

    def acquire(
        self,
        *,
        url: str | None,
        source_key: str | None,
        remote_id: str | None,
        unit: str | None,
        requester_user_id: str | None,
    ) -> dict[str, Any]:
        if (url is None) == (source_key is None or remote_id is None):
            raise CodedRefusal("validation_failed", "give a URL, or a source and a series")
        pools = self._comp.pools()
        if pools is None:
            raise ExitUnavailableError("the job queue is not running")
        acquirer = self.acquirer()

        async def go() -> Any:
            ref = (
                await acquirer.resolve(url)
                if url is not None
                else SeriesRef(source_key=str(source_key), remote_id=str(remote_id), title="")
            )
            tombstones = self._comp.read_context()[2]
            return await acquirer.prepare(
                ref, unit=unit, requester_user_id=requester_user_id, visibility="ops",
                tombstoned=lambda c: bool(tombstones.check(c, root_id=None).blocked),
            )  # fmt: skip

        prepared = self._run(go())
        if prepared.request_id is None:
            return {"state": "stored", "request_id": None, "job_id": None}
        job = pools.submit(
            ACQUIRE_JOB_KIND, dedupe_key=acquire_dedupe_key(prepared.request_id),
            priority_class=REQUEST_CLASS,
        )  # fmt: skip
        return {"state": "queued", "request_id": prepared.request_id, "job_id": job.job_id}
