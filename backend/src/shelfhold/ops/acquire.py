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

from shelfhold.acquisition.application.family_search import (
    FamilyPage,
    FamilyRef,
    FamilyRefs,
    FamilySearch,
    InvalidFamilyRefError,
    empty_page,
)
from shelfhold.acquisition.application.federated_search import (
    FederatedCursors,
    FederatedPage,
    FederatedSearch,
    FederatedSource,
    InvalidFederatedCursorError,
    format_of,
)
from shelfhold.acquisition.public import (
    ACQUIRE_JOB_KIND,
    AcquireRefused,
    NativeAcquirer,
    acquire_dedupe_key,
)
from shelfhold.catalog.public import clean_terms, is_flagged, visible_title
from shelfhold.identity.public import family_profile, family_search_mode, family_source_keys
from shelfhold.infrastructure.fetcher import FetchError
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.ops.tombstone_screens import CatalogScreens
from shelfhold.plugin_host.errors import NoSourceForUrl, RefHasSecret
from shelfhold.plugin_host.settings import SourceSettings
from shelfhold.plugin_host.trust_card import trust_card
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import Capability, SourceError, SourceManifest

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["AcquireOps"]

REQUEST_CLASS = "request"  # EF-INT-017: a person's request, ahead of the catch-up batches


class AcquireOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp
        self._walks = FederatedCursors(comp.clock.now)  # the cursors of the federated search
        # the child's walks are kept apart: an adult's cursor is never a child's
        self._family_walks = FederatedCursors(comp.clock.now)
        self._family_refs = FamilyRefs(comp.clock.now)  # what "request" will resolve (L35)

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

    def federated_search(
        self, query: str, *, owner: str, cursor: str | None = None
    ) -> FederatedPage:
        """One query to every source that is switched on and searches (EF-SRC-005). A source that
        fails is reported, not raised; a cursor that is not ours is `invalid_cursor`."""
        comp = self._comp
        registry = comp.plugins()
        if registry is None:
            raise ExitUnavailableError("the sources are not loaded: the server is not running")
        acquirer = self.acquirer()
        sources: list[FederatedSource] = []
        with comp.engine.connect() as connection:
            for record in registry.active():
                manifest = record.manifest
                if not isinstance(manifest, SourceManifest):
                    continue
                if Capability.SEARCH not in manifest.capabilities:
                    continue
                sources.append(
                    FederatedSource(
                        record.key, manifest.name, format_of(manifest.content_kinds),
                        trust_card(record, connection).as_dict(),
                    )
                )  # fmt: skip
        service = FederatedSearch(acquirer, self._walks)
        try:
            page: FederatedPage = comp.run(
                service.search(sources, query, owner=owner, cursor=cursor)
            )
        except InvalidFederatedCursorError:
            raise CodedRefusal("invalid_cursor", "the cursor is not one this search gave") from None
        return page

    def family_search(self, query: str, *, owner: str, cursor: str | None = None) -> FamilyPage:
        """The family search of a child (EF-USR-014, EF-USR-018): the sources marked "family" that
        her policy leaves open, one title and the action "request" per result, the flagged ones
        dropped before a page is made. The profile is read here, from the database: in "hide" mode,
        or for a reader the rules do not know, not one source is asked."""
        comp = self._comp
        with comp.engine.connect() as connection:
            profile = family_profile(connection, owner)
            if family_search_mode(profile.scope, profile.hide_unrated_remote) != "reduced":  # type: ignore[arg-type]
                return empty_page()
            registry = comp.plugins()
            if registry is None:
                return empty_page()  # no source is loaded: nothing to search, nothing to say
            known = self._family_known(registry)
            open_keys = family_source_keys(connection, owner, "child", known)
        sources = [
            FederatedSource(key, manifest.name, format_of(manifest.content_kinds))
            for key, manifest in known.items()
            if key in open_keys
        ]
        terms = clean_terms(profile.subject_filter)

        def keep(ref: SeriesRef) -> bool:
            """Not flagged, and not a title the catalogue already rated: that one follows the
            catalogue (the one filter of L09), it is never an unrated result."""
            return not is_flagged(ref.title, ref.memo, terms) and not self._classified(ref)

        service = FamilySearch(
            self.acquirer(), self._family_walks, self._family_refs,
            keep=keep, title_of=visible_title,
        )  # fmt: skip
        try:
            page: FamilyPage = comp.run(service.search(sources, query, owner=owner, cursor=cursor))
        except InvalidFederatedCursorError:
            raise CodedRefusal("invalid_cursor", "the cursor is not one this search gave") from None
        return page

    @staticmethod
    def _family_known(registry: Any) -> dict[str, SourceManifest]:
        known: dict[str, SourceManifest] = {}
        for record in registry.active():
            manifest = record.manifest
            if (
                isinstance(manifest, SourceManifest)
                and Capability.SEARCH in manifest.capabilities
                and manifest.content_warning == "safe"  # one that says it is not for children
            ):  # is never asked for a child, whatever the admin ticked
                known[record.key] = manifest
        return known

    def family_door_open(self, owner: str, source_key: str) -> bool:
        """Whether this child may still ask for a result of this source now: the same rules as
        her search, read again (a source taken off the family list, a policy changed, the mode
        set to hide, all close the reference she holds). A request is made at this door only."""
        comp = self._comp
        registry = comp.plugins()
        if registry is None:
            return False
        with comp.engine.connect() as connection:
            profile = family_profile(connection, owner)
            if family_search_mode(profile.scope, profile.hide_unrated_remote) != "reduced":  # type: ignore[arg-type]
                return False
            known = self._family_known(registry)
            return source_key in family_source_keys(connection, owner, "child", known)

    def _classified(self, ref: SeriesRef) -> bool:
        """Whether the catalogue already holds this result as a rated Work or Edition. Its rating
        is the admin's decision: for a child the result is not "unrated", it is hidden (above her
        ceiling) or already in her catalogue (found by the local half), never offered again."""
        item, binding = acquisition.source_item, acquisition.source_binding
        work, edition = catalog.work, catalog.edition
        # A feed source (OPDS and the source built on it) names a book by the search page it is
        # on: `remote_id` is the path of that page, the book is `memo["entry"]`. That identity is
        # stable whatever the query, so it is the one that counts when the result has one.
        entry = ref.memo.get("entry")
        same_book = item.c.remote_id == ref.remote_id
        if isinstance(entry, str) and entry:
            same_book = sa.or_(same_book, sa.func.json_extract(item.c.memo, "$.entry") == entry)
        query = (
            sa.select(sa.literal(1))
            .select_from(
                item.join(binding, binding.c.source_item_id == item.c.id)
                .join(work, work.c.id == binding.c.work_id)
                .join(edition, edition.c.id == binding.c.edition_id)
            )
            .where(
                item.c.source_key == ref.source_key,
                same_book,
                sa.or_(work.c.rated_at.is_not(None), edition.c.rated_at.is_not(None)),
            )
            .limit(1)
        )
        with self._comp.engine.connect() as connection:
            return connection.execute(query).first() is not None

    def unit_count(self, source_key: str, remote_id: str) -> int:
        """What an approval shows before it asks a source for a series (EF-USR-010)."""
        ref = SeriesRef(source_key=source_key, remote_id=remote_id, title="")
        return int(self._run(self.acquirer().count_units(ref)))

    def resolve_family_ref(self, token: str, *, owner: str) -> FamilyRef:
        """What a reference of the family search stands for, for the child it was shown to (the
        request of L35 takes it); `invalid_ref` for any other."""
        try:
            return self._family_refs.resolve(token, owner=owner)
        except InvalidFamilyRefError:
            raise CodedRefusal("invalid_ref", "the reference is not one this search gave") from None

    def acquire(
        self,
        *,
        url: str | None,
        source_key: str | None,
        remote_id: str | None,
        unit: str | None,
        requester_user_id: str | None,
        visibility: str = "ops",
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
                ref, unit=unit, requester_user_id=requester_user_id, visibility=visibility,
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
