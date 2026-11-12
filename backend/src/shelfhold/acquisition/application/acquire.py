"""One-shot acquisition from a native source: search, pasted URL, one file (EF-SRC-015/004).

The flow is SeriesRef -> Unit -> FileAsset -> staging -> ingestion -> export:

1. `search` and `resolve` only read. Both go through the source's own Fetcher (`SourceFetchers`):
   its DomainRules at every hop, `robots.txt`, the per-source HTTP cache. A pasted URL whose host is
   not among the plugin's DomainRules is refused before the plugin sees it.
2. `prepare` makes, in one transaction, the Work (`kind = novel`), the Edition, the `source_item`,
   the binding (its `content_trust` copied from the manifest as the host holds it, that is, after
   the admin's attestation for a third-party plugin), the Unit, the Variant and the persistent
   `download_request` (the requests class of EF-INT-017, with who asked and who may see it).
3. `fetch` runs in a job of the `request` class: the file is streamed into staging, counted and
   hashed on the way, then filed through the store the composition root gives it. The staged hash
   is the one the store checks and keeps; what the bytes are (EPUB or CBZ) is read from the bytes,
   never from the source's name or MIME type, so an EPUB takes the trust sort and the neutralizer
   whatever it was called (EF-BIB-051). When it is filed, `unit.stored` says so.

A refusal that the same bytes or the same source would repeat (a host off the DomainRules,
robots.txt, a reference that holds a secret, an archive off the allowlist, a hash that is not the
one counted) closes the request for good; a transport failure leaves it to be asked again.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.describe import (
    credit_authors,
    incoming_work_ids,
    known_work,
    text_version_for,
    write_rights,
)
from shelfhold.catalog.public import (
    AutoAttachService,
    BindingRef,
    Catalog,
    EditionSpec,
    IncomingEdition,
    IncomingUnit,
    LanguageError,
    RightsService,
    TombstoneScreen,
    UnsupportedFileError,
    announced_kind,
    edition_key_lang,
    file_new_edition,
    translation_type_for,
)
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust import (
    Destination,
    UntrustedEntryError,
    sanitize_upstream_text,
)
from shelfhold.infrastructure.fetcher import (
    DomainNotAllowed,
    FetchError,
    FetchTooLarge,
    RobotsDisallowed,
)
from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, storage
from shelfhold.library.public import (
    SeriesBindingRef,
    VariantAlreadyStoredError,
    absorb_echo,
    receive_cbz,
    sniff_kind,
)
from shelfhold.plugin_host.errors import NoSourceForUrl, RefHasSecret
from shelfhold.plugin_host.refs_guard import RefsGuard, screened, secret_of, values_of
from shelfhold.plugin_host.source_fetcher import SourceFetchers, authenticated
from shelfhold.sdk.models import Cursor, FilterSpec, Page
from shelfhold.sdk.refs import FileAsset, SeriesInfo, SeriesRef, UnitInfo
from shelfhold.sdk.source import (
    Capability,
    ContentRemoved,
    NotConfigured,
    RateLimited,
    SourceError,
    SourceManifest,
    TemporarilyUnavailable,
)

__all__ = [
    "ACQUIRE_JOB_KIND",
    "AcquireOutcome",
    "AcquireRefused",
    "NativeAcquirer",
    "OwnExportEcho",
    "Prepared",
    "SourceDirectory",
    "acquire_dedupe_key",
]

log = structlog.get_logger(__name__)

ACQUIRE_JOB_KIND = "acquire.run"
WORK_KIND = "novel"
TITLE_MAX = 200
RIGHTS_MAX = 2000
UNKNOWN_LANGUAGE = "x-unknown"
ADULT_FROM = 18
OPEN_STATES = ("requested", "downloaded")
# What the host takes of a file handed over whole: images in an archive, or an EPUB. A PDF waits
# for the import folder (M2 L20, D9), audio for its own edition key.
TAKEN_FILE_KEY = "file"


def acquire_dedupe_key(request_id: str) -> str:
    return f"{ACQUIRE_JOB_KIND}:{request_id}"


class OwnExportEcho(Exception):
    """The file is an export of Shelfhold's own: it was recorded as a replica of
    `export_item_id` and the request that asked for it is gone (I50)."""

    def __init__(self, export_item_id: str) -> None:
        super().__init__(export_item_id)
        self.export_item_id = export_item_id


class AcquireRefused(Exception):
    """A refusal under a catalog code (never the text of the source)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class SourceDirectory(Protocol):
    """The plugin registry, as acquisition needs it."""

    def active(self) -> list[Any]: ...

    def get(self, key: str) -> Any: ...

    async def resolve_url(self, url: str, fetcher_for: Callable[[Any], Any]) -> Any: ...


class Filer(Protocol):
    """The store a received file goes to (`PassthroughStore`, or the EPUB store by its suffix)."""

    def ingest(self, **arguments: Any) -> Any: ...


Place = Callable[[str, str], str]  # (variant id, ".cbz" | ".epub") -> local path in /library


@dataclass(frozen=True)
class Prepared:
    request_id: str | None  # None: the Variant already has its file, nothing is asked
    work_id: str
    edition_id: str
    unit_id: str
    variant_id: str
    created: bool  # a new Work was made for it


@dataclass(frozen=True)
class AcquireOutcome:
    state: str  # fetched | abandoned | waiting | stored | replica
    request_id: str
    reason: str | None = None
    sha256: str | None = None
    kind: str | None = None
    event: PublishedEvent | None = None


class _Handle:
    def __init__(
        self,
        service: Any,
        fetcher: Any,
        record: Any,
        guard: RefsGuard | None = None,
        guarded: Any = None,
    ) -> None:
        self.guard = guard or RefsGuard(values_of(fetcher))
        self.service = service
        self.fetcher = fetcher  # what the plugin is handed (the guarded one, or the wrapper)
        self.guarded = guarded if guarded is not None else fetcher  # streams, and is closed
        self.record = record

    async def aclose(self) -> None:
        close = getattr(self.guarded, "aclose", None)
        if close is not None:
            await close()


def _flat(specs: list[FilterSpec]) -> set[str]:
    names: set[str] = set()
    for spec in specs:
        names.add(spec.key)
        names |= _flat(spec.children)
    return names


class NativeAcquirer:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        bus: EventBus,
        directory: SourceDirectory,
        fetchers: SourceFetchers,
        *,
        staging_dir: Path,
        max_bytes: int,
        source_settings: Callable[[str], Mapping[str, object]] | None = None,
        source_secrets: Callable[[str], Mapping[str, str]] | None = None,
        connected: Callable[[str], Any] | None = None,
        work_screen: Callable[[], TombstoneScreen] | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._bus = bus
        self._directory = directory
        self._fetchers = fetchers
        self._staging = staging_dir
        self._max = max_bytes
        self._settings = source_settings or (lambda _key: {})
        self._secrets = source_secrets or (lambda _key: {})  # the sealed secrets of a source (L07)
        self._connected = connected  # the connected service (Suwayomi): search only
        # The Work tombstones (EF-BIB-053): asked for the identifiers a source gives for its Work,
        # opened before the write transaction (the replay of the journal may write)
        self._work_screen = work_screen

    # -- opening a source ---------------------------------------------------------------------

    def _record(self, key: str) -> Any:
        record = self._directory.get(key)
        if record is None or record not in self._directory.active():
            raise AcquireRefused("plugin.unknown", "no active source has that key")
        if not isinstance(record.manifest, SourceManifest):
            raise AcquireRefused("plugin.unknown", "that plugin is not a source")
        self._configured(record)
        return record

    def _configured(self, record: Any) -> None:
        """A source whose manifest requires a setting is not switched on until it has a value
        (D6: Gutenberg needs its mirror; no default address is written in the code)."""
        required = record.manifest.settings_schema.get("required", [])
        given = self._settings(record.key)
        missing = [k for k in required if not str(given.get(k) or "").strip()]
        if missing:
            raise AcquireRefused("source.not_configured", "a required setting is not set")

    async def _open(self, record: Any) -> _Handle:
        settings = self._settings(record.key)
        guarded = self._fetchers.for_record(record, settings)
        try:
            fetcher = authenticated(guarded, record.manifest, settings, self._secrets(record.key))
            instance = record.instance
            configure = getattr(instance, "configured", None)
            if configure is not None:  # a source bound to an address the admin gave (plain values)
                instance = configure(settings)
        except BaseException:
            await guarded.aclose()
            raise
        service = screened(
            instance,
            values_of(fetcher),
            lambda: getattr(fetcher, "ref_template", None),
            secret_of(fetcher),
        )
        guard = RefsGuard(values_of(fetcher), secret=secret_of(fetcher))
        return _Handle(service, fetcher, record, guard, guarded)

    async def _search_handle(self, source_key: str) -> tuple[_Handle, str | None]:
        key, _, sub = source_key.partition(":")
        if self._connected is not None:
            link = self._connected(key)
            if link is not None:
                return _Handle(link.service, link.guarded, None), sub or None
        return await self._open(self._record(source_key)), None

    # -- reading ------------------------------------------------------------------------------

    def filters(self, source_key: str) -> list[FilterSpec]:
        """The filters the source's search takes (`Source.filters`)."""
        key = source_key.partition(":")[0]
        if self._connected is not None:
            link = self._connected(key)
            if link is not None:
                return list(link.service.filters())
        return list(self._record(source_key).instance.filters())

    async def search(
        self,
        source_key: str,
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        cursor: str | None = None,
    ) -> Page[SeriesRef]:
        wanted = dict(filters or {})
        if wanted and not set(wanted) <= _flat(self.filters(source_key)):
            raise AcquireRefused("source.filter_invalid", "a filter the source does not declare")
        handle, sub = await self._search_handle(source_key)
        record = handle.record
        if record is not None and Capability.SEARCH not in record.manifest.capabilities:
            raise AcquireRefused("plugin.unknown", "that source does not search")
        try:
            page: Page[SeriesRef] = await handle.service.search(
                handle.fetcher, query, wanted, Cursor(token=cursor) if cursor else None, sub=sub
            )
            return page
        except NotConfigured as error:
            raise AcquireRefused(
                "source.not_configured", "the source's settings are refused"
            ) from error
        finally:
            await handle.aclose()

    async def resolve(self, url: str) -> SeriesRef:
        """The series behind a pasted URL (EF-SRC-004), under the DomainRules of the plugin that
        recognised it: a URL of another host is refused before the plugin is called."""
        opened: list[Any] = []

        def fetcher_for(record: Any) -> Any:
            host = urlsplit(url).hostname or ""
            if not self._fetchers.allowlist(record, self._settings(record.key)).allows(host):
                raise DomainNotAllowed("the pasted URL is outside the DomainRules of its source")
            fetcher = self._fetchers.for_record(record, self._settings(record.key))
            opened.append(fetcher)
            return fetcher

        try:
            resolution = await self._directory.resolve_url(url, fetcher_for)
            ref = resolution.ref
            if not isinstance(ref, SeriesRef):
                raise NoSourceForUrl()  # a Unit alone has no series to file it under (yet)
            guard = RefsGuard(values_of(opened[0]), template=None, secret=secret_of(opened[0]))
            return guard.screen(ref)
        finally:
            for fetcher in opened:
                await fetcher.aclose()

    # -- the request --------------------------------------------------------------------------

    async def prepare(
        self,
        ref: SeriesRef,
        *,
        unit: str | None = None,
        requester_user_id: str | None = None,
        visibility: str = "ops",
        shelf_id: str | None = None,
        tombstoned: Callable[[SeriesBindingRef], bool] | None = None,
    ) -> Prepared:
        record = self._record(ref.source_key)
        manifest: SourceManifest = record.manifest
        if TAKEN_FILE_KEY not in manifest.content_kinds:
            raise AcquireRefused("acquire.unsupported_file", "that source hands over no file")
        if tombstoned is not None and tombstoned(
            SeriesBindingRef(record.key, ref.source_key, ref.remote_id)
        ):
            raise AcquireRefused("acquire.series_tombstoned", "the series was deleted on purpose")
        handle = await self._open(record)
        try:
            # a reference given by the caller is screened like the ones a source hands back: the
            # route and the command take it from a person, and it is stored (EF-SRC-035)
            ref = handle.guard.screen(ref)
            info = await handle.service.get_series(handle.fetcher, ref)
            units = await handle.service.list_units(handle.fetcher, ref)
        except NotConfigured as error:
            raise AcquireRefused(
                "source.not_configured", "the source's settings are refused"
            ) from error
        finally:
            await handle.aclose()
        chosen = self._choose(units, unit)
        screen = self._screen_for(record, info)
        return self._record_request(
            record, manifest, ref, info, chosen,
            requester_user_id=requester_user_id, visibility=visibility, shelf_id=shelf_id,
            screen=screen,
        )  # fmt: skip

    def _screen_for(self, record: Any, info: SeriesInfo) -> TombstoneScreen | None:
        """The Work tombstones, when the source gives identifiers for its Work: no screen to ask
        means no arrival, never an unscreened one (fails closed)."""
        if not incoming_work_ids(record.key, info):
            return None
        if self._work_screen is None:
            raise AcquireRefused(
                "acquire.tombstones_unavailable", "the Work tombstones cannot be checked here"
            )
        return self._work_screen()

    @staticmethod
    def _choose(units: list[UnitInfo], wanted: str | None) -> UnitInfo:
        free = [u for u in units if u.availability in ("free", "purchased")]
        if wanted is not None:
            found = [u for u in free if u.remote_unit_id == wanted]
            if not found:
                raise AcquireRefused("source.content_removed", "that Unit is not offered")
            return found[0]
        if len(free) != 1:
            raise AcquireRefused("acquire.unit_required", "say which Unit to take")
        return free[0]

    def _record_request(
        self,
        record: Any,
        manifest: SourceManifest,
        ref: SeriesRef,
        info: SeriesInfo,
        unit: UnitInfo,
        *,
        requester_user_id: str | None,
        visibility: str,
        shelf_id: str | None,
        screen: TombstoneScreen | None = None,
    ) -> Prepared:
        now = self._clock.now()
        title = info.title or ref.title
        announced: list[PublishedEvent] = []
        with self._engine.connect() as connection:
            connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            with connection.begin():
                binding_id, work_id, edition_id, created = self._binding(
                    connection, record, manifest, ref, title, unit, shelf_id, now, info, screen
                )
                if created:  # the warning of the rights review is announced, never blocking
                    announced = self._review_rights(connection, edition_id, now)
                ingested = Catalog(connection).ingest(
                    IncomingUnit(
                        binding=BindingRef(binding_id, edition_id),
                        remote_unit_id=unit.remote_unit_id, raw_number=unit.number, seen_at=now,
                        source_order=unit.source_order,
                        lang=unit.lang, revision_hint=unit.revision_hint,
                        title=_clean(unit.title or title), kind="book",
                        published_on=unit.published_on, date_precision=unit.date_precision,
                        published_at=unit.published_at,
                    )
                )  # fmt: skip
                stored = connection.execute(
                    sa.select(storage.content.c.id).where(
                        storage.content.c.variant_id == ingested.variant_id
                    )
                ).first()
                if stored is not None:
                    self._bus.deliver(announced)
                    return Prepared(None, work_id, edition_id, ingested.unit_id,
                                    ingested.variant_id, created)  # fmt: skip
                request_id = self._open_request(
                    connection, binding_id, ingested.variant_id, requester_user_id, visibility, now
                )
        self._bus.deliver(announced)
        return Prepared(request_id, work_id, edition_id, ingested.unit_id, ingested.variant_id,
                        created)  # fmt: skip

    def _review_rights(
        self, connection: Connection, edition_id: str, now: datetime
    ) -> list[PublishedEvent]:
        """The rights review of a new Edition (EF-SRC-024): for the country of the instance, with
        what the source gave. It announces a work that may still be protected there and blocks
        nothing."""
        events: list[PublishedEvent] = []

        class _Announce:
            def __init__(self, bus: EventBus) -> None:
                self._bus = bus

            def publish(self, conn: Connection, event_type: str, payload: Any = None) -> Any:
                published = self._bus.publish(conn, event_type, payload)
                events.append(published)
                return published

        rights = RightsService(connection, _Announce(self._bus))
        rights.refresh_cache(edition_id)  # the death years the credits give, before the review
        rights.review(edition_id, today=now.date())
        return events

    def _binding(
        self,
        connection: Connection,
        record: Any,
        manifest: SourceManifest,
        ref: SeriesRef,
        title: str,
        unit: UnitInfo,
        shelf_id: str | None,
        now: datetime,
        info: SeriesInfo,
        screen: TombstoneScreen | None = None,
    ) -> tuple[str, str, str, bool]:
        item, binding = acquisition.source_item, acquisition.source_binding
        found = connection.execute(
            sa.select(binding.c.id, binding.c.work_id, binding.c.edition_id)
            .select_from(binding.join(item, item.c.id == binding.c.source_item_id))
            .where(
                item.c.plugin_id == record.key,
                item.c.source_key == ref.source_key,
                item.c.remote_id == ref.remote_id,
            )
        ).first()
        if found is not None:
            return found.id, found.work_id, found.edition_id, False
        books = Catalog(connection)
        ids = incoming_work_ids(record.key, info)
        if ids and screen is not None and screen.work_ids(ids):
            # a Work purged on purpose does not come back by an identifier it carried (EF-BIB-053):
            # asked before anything is made, so that nothing is left of the refusal
            raise AcquireRefused("acquire.work_tombstoned", "the work was deleted on purpose")
        # a book of a source that groups its books joins the Work its sibling made (EF-BIB-049)
        work_id = known_work(connection, record.key, info) or books.create_work(
            WORK_KIND, _title(title, ref.remote_id)
        )
        lang = _language(unit.lang or (manifest.langs[0] if manifest.langs else None))
        translation = translation_type_for(manifest.translation_nature)
        version, translator = (
            text_version_for(connection, info, _clean)
            if manifest.translation_nature == "published"
            else (None, None)
        )
        publisher = _clean(info.publisher) or ""
        if lang is None:
            edition_id = new_ulid()
            connection.execute(
                sa.insert(catalog.edition).values(
                    id=edition_id, work_id=work_id, lang=UNKNOWN_LANGUAGE,
                    translation_type=translation, media="text",
                )
            )  # fmt: skip
        else:
            edition_id = books.edition_for_translation(
                EditionSpec(
                    work_id,
                    lang,
                    translation_type=translation,
                    media="text",
                    publisher=publisher,
                ),
                kind=WORK_KIND,
                nature=manifest.translation_nature,
                text_version=version,
            ).id
        write_rights(
            connection, edition_id, basis=manifest.rights_basis,
            jurisdiction=manifest.rights_jurisdiction, attribution=_attribution(info.rights),
            plugin_key=record.key, now=now,
        )  # fmt: skip
        # the one door of the shelf and of the identifiers of a new Edition: a shelf below 18+
        # never takes it unrated, and the Work tombstones are asked again under the lock
        shelf = shelf_id or _shelf(connection)
        if ids:
            if screen is None:  # pragma: no cover (`prepare` refuses it before the transaction)
                raise AcquireRefused("acquire.tombstones_unavailable")
            arrival = AutoAttachService(tombstones=screen).arrive(
                connection, IncomingEdition(edition_id, shelf, ids), now=now
            )
            if "tombstoned" in arrival.conflicts:
                raise AcquireRefused("acquire.work_tombstoned", "the work was deleted on purpose")
            attached = arrival.work_id != work_id  # an identifier it carried named another Work
            work_id = arrival.work_id
        else:
            file_new_edition(connection, edition_id, shelf)
            attached = False
        if not attached:  # an Edition attached to a known Work leaves that Work's credits alone
            credit_authors(connection, work_id, info, _clean)
        if translator is not None:
            books.credit_edition(edition_id, translator, "trl")
        item_id = new_ulid()
        connection.execute(
            sa.insert(item).values(
                id=item_id, plugin_id=record.key, source_key=ref.source_key,
                remote_id=ref.remote_id, memo=ref.memo, memo_version=ref.memo_version,
                last_seen=now,
            )
        )  # fmt: skip
        binding_id = new_ulid()
        connection.execute(
            sa.insert(binding).values(
                id=binding_id, work_id=work_id, edition_id=edition_id, source_item_id=item_id,
                priority=1, active=True,
                # as the manifest stands now: a third-party plugin's value only after attestation
                content_trust=manifest.content_trust,
            )
        )  # fmt: skip
        return binding_id, work_id, edition_id, True

    @staticmethod
    def _open_request(
        connection: Connection,
        binding_id: str,
        variant_id: str,
        requester_user_id: str | None,
        visibility: str,
        now: datetime,
    ) -> str:
        table = acquisition.download_request
        unit_id = connection.execute(
            sa.select(catalog.unit_variant.c.unit_id).where(catalog.unit_variant.c.id == variant_id)
        ).scalar()
        open_one = connection.execute(
            sa.select(table.c.id).where(
                sa.or_(table.c.variant_id == variant_id, table.c.unit_id == unit_id),
                table.c.state.in_(OPEN_STATES),
            )
        ).scalar()  # one open request per Unit (I52): the follow's own is reused
        if open_one is not None:
            return str(open_one)
        request_id = new_ulid()
        connection.execute(
            sa.insert(table).values(
                id=request_id, target_kind="unit", variant_id=variant_id,
                source_binding_id=binding_id, state="requested", requested_at=now,
                unit_id=unit_id, visibility=visibility, requester_user_id=requester_user_id,
            )
        )  # fmt: skip
        return request_id

    # -- the file -----------------------------------------------------------------------------

    async def fetch(self, request_id: str, filer: Filer, place: Place) -> AcquireOutcome:
        row = self._load(request_id)
        if row is None or row.state not in OPEN_STATES:
            return AcquireOutcome("stored", request_id, "nothing_to_do")
        handle: _Handle | None = None
        try:
            record = self._record(row.source_key)
            handle = await self._open(record)
            ref = SeriesRef(
                source_key=row.source_key, remote_id=row.remote_id, title="",
                memo=row.memo or {}, memo_version=row.memo_version,
            )  # fmt: skip
            units = await handle.service.list_units(handle.fetcher, ref)
            unit = next((u for u in units if u.remote_unit_id == row.remote_unit_id), None)
            if unit is None:
                raise ContentRemoved("the Unit is no longer offered")
            payload = await handle.service.fetch_unit(handle.fetcher, ref, unit)
            asset = _taken(payload)
            sha, kind = await self._receive(handle, asset, row, filer, place)
        except OwnExportEcho:
            return AcquireOutcome("replica", row.id, "own_export")
        except (RateLimited, TemporarilyUnavailable) as error:  # asked again, nothing lost
            self._bump(row.id)
            log.info("acquire.waiting", request_id=row.id, error=type(error).__name__)
            return AcquireOutcome("waiting", row.id, type(error).__name__)
        except (
            AcquireRefused,
            DomainNotAllowed,
            RobotsDisallowed,
            RefHasSecret,
            UnsupportedFileError,
            UntrustedEntryError,
            ContentRemoved,
            NotConfigured,
            FetchTooLarge,
            InvalidFetchUrl,
            SourceError,  # the source's own refusals (auth, changed, not configured, removed)
        ) as error:
            return self._close(row, "abandoned", error)
        except ValueError as error:  # `received_hash_mismatch`: not the bytes that were counted
            return self._close(row, "abandoned", error)
        # asleep, unreachable, too slow, robots.txt unreadable (an outage, not a refusal): the
        # request is kept and asked again, nothing lost
        except FetchError as error:
            self._bump(row.id)
            log.info("acquire.waiting", request_id=row.id, error=type(error).__name__)
            return AcquireOutcome("waiting", row.id, type(error).__name__)
        finally:
            if handle is not None:
                await handle.aclose()
        return self._fetched(row, sha, kind)

    async def _receive(
        self, handle: _Handle, asset: FileAsset, row: Any, filer: Filer, place: Place
    ) -> tuple[str, str]:
        request = asset.request
        if request.method != "GET" or request.body is not None:
            raise AcquireRefused("acquire.unsupported_file", "a file is asked for with a GET")
        # the host and the User-Agent are the Fetcher's, never the source's (ENF-LEG-007)
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("user-agent", "host")
        }
        url = request.url
        authorize = getattr(handle.fetcher, "authorize", None)
        if authorize is not None:
            # the credential of the source, towards the origin it was declared for and no other
            url, headers = authorize(url, headers)
        async with receive_cbz(
            handle.guarded, url, staging_dir=self._staging, max_bytes=self._max,
            headers=headers,
        ) as received:  # fmt: skip
            kind = sniff_kind(received.path)  # the bytes decide, not the name or the MIME type
            # I50: what Shelfhold exported comes back as a replica, never as a Variant
            echo = absorb_echo(
                self._engine, request_id=row.id, staged=received.path, kind=kind,
                file_hash=received.sha256, size=received.size, remote_ref=row.remote_unit_id,
            )  # fmt: skip
            if echo is not None:
                raise OwnExportEcho(echo.own.export_item_id)
            with suppress(VariantAlreadyStoredError):  # a replay after a crash: already there
                filer.ingest(
                    unit_id=row.unit_id, variant_id=row.variant_id, staged=received.path,
                    expected_sha256=received.sha256, dest=place(row.variant_id, kind),
                    upstream_ref=row.remote_unit_id,
                )  # fmt: skip
            return received.sha256, kind.removeprefix(".")

    # -- the ticket ---------------------------------------------------------------------------

    def _load(self, request_id: str) -> Any:
        r, v, b, i = (
            acquisition.download_request, catalog.unit_variant, acquisition.source_binding,
            acquisition.source_item,
        )  # fmt: skip
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(
                    r.c.id,
                    r.c.state,
                    r.c.visibility,
                    r.c.requester_user_id,
                    r.c.source_binding_id,
                    v.c.id.label("variant_id"),
                    v.c.unit_id,
                    v.c.remote_unit_id,
                    b.c.edition_id,
                    b.c.work_id,
                    i.c.plugin_id,
                    i.c.source_key,
                    i.c.remote_id,
                    i.c.memo,
                    i.c.memo_version,
                )
                .select_from(
                    r.join(v, v.c.id == r.c.variant_id)
                    .join(b, b.c.id == r.c.source_binding_id)
                    .join(i, i.c.id == b.c.source_item_id)
                )
                .where(r.c.id == request_id)
            ).first()

    def _bump(self, request_id: str) -> None:
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == request_id)
                .values(attempts=table.c.attempts + 1, updated_at=self._clock.now())
            )

    def _close(self, row: Any, state: str, error: Exception) -> AcquireOutcome:
        """The same answer would come again: closed for good, said by its class, never by the
        source's text (it may name a title or carry what it was handed)."""
        reason = _reason(error)
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == row.id)
                .values(state=state, updated_at=self._clock.now())
            )
        log.warning("acquire.refused", request_id=row.id, reason=reason)
        return AcquireOutcome(state, row.id, reason)

    def _fetched(self, row: Any, sha: str, kind: str) -> AcquireOutcome:
        table = acquisition.download_request
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.id == row.id)
                .values(state="fetched", updated_at=self._clock.now())
            )
            published = self._bus.publish(
                connection, "unit.stored",
                {"unit_id": row.unit_id, "edition_id": row.edition_id, "work_id": row.work_id,
                 "kind": kind},
                actor_id=row.requester_user_id, visibility=row.visibility or "ops",
            )  # fmt: skip
        self._bus.deliver([published])
        return AcquireOutcome("fetched", row.id, None, sha, kind, published)


# -- helpers ------------------------------------------------------------------------------------


def _taken(payload: Any) -> FileAsset:
    if not isinstance(payload, FileAsset):
        raise AcquireRefused("acquire.unsupported_file", "the source did not hand over a file")
    announced_kind(payload.mime)  # a PDF, audio...: refused here, before a byte is fetched
    return payload


def _reason(error: Exception) -> str:
    if isinstance(error, AcquireRefused):
        return error.code
    if isinstance(error, DomainNotAllowed):
        return "source.domain_not_allowed"
    if isinstance(error, RobotsDisallowed):
        return "fetch.robots_disallowed"
    if isinstance(error, RefHasSecret):
        return "source.ref_has_secret"
    if isinstance(error, UnsupportedFileError):
        return "acquire.unsupported_file"
    if isinstance(error, UntrustedEntryError):
        return "content.untrusted_entry"
    if isinstance(error, NotConfigured):
        return "source.not_configured"
    if isinstance(error, ContentRemoved):
        return "source.content_removed"
    if isinstance(error, FetchTooLarge):
        return "fetch.too_large"
    if isinstance(error, InvalidFetchUrl):
        return "source.credential_origin"  # the credential would have left its origin
    if isinstance(error, SourceError):
        return str(error.code)
    return "received_hash_mismatch" if "received_hash_mismatch" in str(error) else "refused"


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    clean = sanitize_upstream_text(text, destination=Destination.PLAIN, max_length=TITLE_MAX)
    return clean.strip() or None


def _attribution(text: str | None) -> str | None:
    """The attribution a source gave, as plain bounded text (it is copied into a file)."""
    if text is None:
        return None
    clean = sanitize_upstream_text(text, destination=Destination.PLAIN, max_length=RIGHTS_MAX)
    return clean.strip() or None


def _title(title: str, remote_id: str) -> str:
    for text in (title, remote_id):
        clean = _clean(text)
        if clean:
            return clean
    return "?"


def _language(raw: str | None) -> str | None:
    if raw is None or raw == "mul":
        return None
    try:
        return str(edition_key_lang(raw))
    except LanguageError:
        return None


def _shelf(connection: Connection) -> str | None:
    """The first shelf with an enabled destination and no age ceiling below 18+: where a request
    is filed unless the admin names another."""
    shelf, destination = export.shelf, export.shelf_destination
    return connection.execute(
        sa.select(shelf.c.id)
        .join(destination, destination.c.shelf_id == shelf.c.id)
        .where(
            destination.c.enabled,
            sa.or_(shelf.c.max_age_rating.is_(None), shelf.c.max_age_rating >= ADULT_FROM),
        )
        .order_by(shelf.c.id)
        .limit(1)
    ).scalar()
