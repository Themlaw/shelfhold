"""`meta rebuild|check` (ENF-OPS-020, EF-EXP-063): the projections of the database, written again.

`unit.json` and `work.json` in `/data/meta`, and the provenance mirror of every tree destination in
`<root>/.shelfhold/manifest/`. The database is the authority; none of them is read to learn
anything: a mirror line the database cannot explain stays as it is, flagged `unresolved`, and no
identity, Unit or row is ever created from an identifier it carries (the mirror is an untrusted
file). The command runs in the instance (the socket of the server, else the instance lock), and
the mirror is written under the write lease like any write to a library root, through the intent
journal. `check` writes nothing.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.catalog.application.enrich import DAILY_QUOTA
from shelfhold.catalog.application.release_calendar import ReleaseCalendar
from shelfhold.catalog.public import EnrichReport, EnrichService, UnknownTargetError
from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.identity.domain.visibility import ALL
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher.quota_store import SqlBucketStore
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.provider_keys import (
    PROVIDERS as KEYED_PROVIDERS,
)
from shelfhold.infrastructure.secrets.provider_keys import (
    ProviderKey,
    clear_provider_key,
    load_provider_key,
    provider_key_present,
    store_provider_key,
)
from shelfhold.library.public import MetaProjection
from shelfhold.ops.errors import ExitUnavailableError, UnknownSubjectError
from shelfhold.ops.library import refusal_of
from shelfhold.plugin_host import provider_fetcher
from shelfhold.plugin_host.adapters.native.anilist import AniListProvider
from shelfhold.plugin_host.adapters.native.bnf import BnFProvider
from shelfhold.plugin_host.adapters.native.comicvine import NOTICE, ComicVineProvider
from shelfhold.plugin_host.adapters.native.googlebooks import GoogleBooksProvider
from shelfhold.plugin_host.adapters.native.hardcover import HardcoverProvider
from shelfhold.plugin_host.adapters.native.mangaupdates import MangaUpdatesProvider
from shelfhold.plugin_host.adapters.native.openlibrary import OpenLibraryProvider
from shelfhold.plugin_host.adapters.native.wikidata import WikidataProvider
from shelfhold.plugin_host.bnf_sru import read_sru
from shelfhold.sdk.metadata import MatchHints, MetadataProvider

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["MetaOps"]


def _report(report: EnrichReport) -> dict[str, Any]:
    def named(keys: Any) -> list[str]:
        return [k.field for k in keys]

    return {
        "outcome": report.outcome, "reason": report.reason, "edition_id": report.edition_id,
        "written": named(report.written), "kept_locked": named(report.kept_locked),
        "kept_manual": named(report.kept_manual),
        "ages": [{"age": a.min_age, "x": a.x} for a in report.age_suggested],
        "withdrawals": len(report.withdrawals),
        "cover": report.cover, "cover_rejected": report.cover_rejected,
        "attribution": report.attribution, "source_url": report.source_url,
        "suggestions": [
            {"provider": s.provider, "url": s.url, "correspondences": dict(s.correspondences)}
            for s in report.suggestions
        ],
    }  # fmt: skip


# The providers an operator may ask. OpenLibrary answers by ISBN; AniList, MangaUpdates and
# Wikidata by title; the BnF by title or, without one, by the ISBN of the Edition. AniList is only
# ever asked (a lookup) and Wikidata only gives suggestions: the registry holds no account, no
# token, no list.
PROVIDERS: dict[str, Callable[[], MetadataProvider]] = {
    "openlibrary": OpenLibraryProvider,
    "anilist": AniListProvider,
    "mangaupdates": MangaUpdatesProvider,
    "bnf": lambda: BnFProvider(reader=read_sru),  # the host's own reader of XML (ENF-SEC-014)
    "wikidata": WikidataProvider,
    "comicvine": ComicVineProvider,
    "hardcover": HardcoverProvider,  # the instance's own token, sealed (`provider_secret`)
    "googlebooks": GoogleBooksProvider,  # the instance's own key, sealed
}
BY_TITLE = frozenset({"anilist", "mangaupdates", "wikidata", "comicvine"})  # a title is required
TITLE_OPTIONAL = frozenset({"bnf", "hardcover", "googlebooks"})  # a title when given, else the ISBN
# Where the host goes when a provider cannot answer for the rest of its day (its budget is spent, or
# it said «Queries per day» and is suspended until Pacific midnight): the first of these that
# enriches the Edition answers instead.
FALLBACKS: dict[str, tuple[str, ...]] = {"googlebooks": ("openlibrary", "bnf")}


class MetaOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _destinations(self) -> list[str]:
        table = export.shelf_destination
        with self._comp.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(table.c.id).where(table.c.transport == "tree").order_by(table.c.id)
                ).scalars()
            )

    def _mirror(self, fsops: Any) -> ProvenanceMirror:
        return ProvenanceMirror(
            self._comp.engine, fsops, clock=self._comp.clock, operating_bus=self._comp.bus
        )

    def _projection(self) -> MetaProjection:
        return MetaProjection(self._comp.engine, self._comp.settings.data_dir)

    def _drifted_mirror_files(self) -> tuple[dict[str, list[str]], list[str]]:
        """Per destination, the mirror files missing or out of date, and the ones left alone
        (unreadable, a link, a newer major)."""
        destinations = self._destinations()
        if not destinations:
            return {}, []
        fsops, _loaded, _tombstones = self._comp.read_context()
        mirror = self._mirror(fsops)
        found = {d: names for d in destinations if (names := mirror.drifted(d))}
        return found, list(mirror.skipped)

    @staticmethod
    def _pasted(target: str) -> str:
        """An OpenLibrary address pasted as the target names the book it points at: its ISBN."""
        ids = OpenLibraryProvider().ids_from_url(target.strip())
        if ids is None:
            return target
        isbn = ids.get("isbn13") or ids.get("isbn10")
        if isbn is None:  # an address by OpenLibrary key: the catalog holds no Edition under it
            raise UnknownSubjectError("that address names no ISBN: give the ISBN or the Edition")
        return isbn

    def enrich(
        self, target: str, provider: str = "openlibrary", title: str | None = None
    ) -> dict[str, Any]:
        """`meta enrich <isbn|edition>`: ask a provider about one Edition (EF-BIB-013).

        The proposals are stored with their provenance and the age only as a suggestion; a
        provider that is down is a notification and a refusal of this command, nothing else.
        A provider that answers by title (`BY_TITLE`) needs `title` (the BnF takes one when
        given); the release calendar of the series is kept at the same time when the provider
        gives one. A provider that only suggests (Wikidata) records nothing: the report says it."""
        settings = self._comp.settings
        if provider not in PROVIDERS:
            raise UnknownSubjectError(f"no such provider: {provider}")
        if provider == "comicvine":
            self._comicvine_ready()
        if provider in KEYED_PROVIDERS:
            self._key_ready(provider)
        if provider in BY_TITLE and not (title and title.strip()):
            raise UnknownSubjectError(f"{provider} is asked by title: give --title")
        target = self._pasted(target) if provider == "openlibrary" else target
        service = EnrichService(
            self._comp.engine, self._comp.clock, self._comp.bus, data_dir=settings.data_dir
        )
        try:
            with self._comp.engine.connect() as connection:
                edition_id = service.resolve(connection, target)
            report = asyncio.run(self._enrich(service, edition_id, provider, title))
        except (UnknownTargetError, LookupError) as error:
            raise UnknownSubjectError(str(error)) from None
        done = _report(report)
        done["provider"] = provider
        until = SqlBucketStore(self._comp.engine).suspended_until(provider, time.time())
        done["suspended_until"] = (
            datetime.fromtimestamp(until, UTC).isoformat(timespec="minutes") if until else None
        )  # a provider that said its day is spent: when it is asked again
        if (fallback := self._fallback(service, edition_id, provider, title, report)) is not None:
            used, answered = fallback
            suspended = done["suspended_until"]
            done = _report(answered)
            done["suspended_until"] = suspended
            done["provider"] = used
            done["fallback_of"] = provider
            done["fallback_reason"] = report.reason or report.outcome
        if provider == "comicvine":
            done["notice"] = NOTICE  # the terms are said with every answer, not only the first
        return done

    def _fallback(
        self,
        service: EnrichService,
        edition_id: str,
        provider: str,
        title: str | None,
        report: EnrichReport,
    ) -> tuple[str, EnrichReport] | None:
        """The report of the first other provider that enriches, when `provider` cannot answer
        for the rest of its day; `None` when it can, or when nobody else enriches."""
        spent = report.outcome == "quota" or (
            report.outcome == "provider_down" and report.reason == DAILY_QUOTA
        )
        if not spent:
            return None
        for other in FALLBACKS.get(provider, ()):
            try:
                answered = asyncio.run(self._enrich(service, edition_id, other, title))
            except (UnknownTargetError, LookupError):  # this one has nothing to ask by
                continue
            if answered.outcome == "enriched":
                return other, answered
        return None

    def _key_ready(self, provider: str) -> None:
        """Hardcover and Google Books are asked with the key of the instance, sealed: without
        one nothing is asked, and the refusal says how to give it."""
        if not self._key_present(provider):
            raise ExitUnavailableError(
                f"{provider} needs the key of this instance: give it with "
                f"`shelfhold meta provider-key set {provider}` (the key is read from stdin)"
            )

    def _key_present(self, provider: str) -> bool:
        with self._comp.engine.connect() as connection:
            return provider_key_present(connection, provider)

    def provider_key_set(self, provider: str, key: str) -> dict[str, Any]:
        """Seal `key` for `provider` (the operator's, local, from its standard input)."""
        if provider not in KEYED_PROVIDERS:
            raise UnknownSubjectError(f"no key is kept for {provider}")
        if provider == "hardcover":
            key = re.sub(r"^bearer\s+", "", key, flags=re.IGNORECASE)  # the site shows the scheme
        try:
            sealed = ProviderKey(key)
        except ValueError as error:
            raise UnknownSubjectError(str(error)) from None
        vault = self._comp.vault()
        with write_transaction(self._comp.engine) as connection:
            registry.register_active_key(connection, vault.keyring)
            store_provider_key(vault, connection, provider, sealed)
        return {"provider": provider, "set": True}

    def provider_key_clear(self, provider: str) -> dict[str, Any]:
        if provider not in KEYED_PROVIDERS:
            raise UnknownSubjectError(f"no key is kept for {provider}")
        with write_transaction(self._comp.engine) as connection:
            return {"provider": provider, "cleared": clear_provider_key(connection, provider)}

    def provider_key_status(self) -> dict[str, bool]:
        """Which providers have a key: presence only, never the key."""
        return {name: self._key_present(name) for name in KEYED_PROVIDERS}

    def _comicvine_ready(self) -> None:
        """ComicVine is off until the operator switches it on and brings the key of the instance
        (EF-BIB-013): otherwise nothing is asked, and the terms are what the refusal says."""
        settings = self._comp.settings
        if not settings.comicvine_enabled:
            raise ExitUnavailableError(
                "comicvine is off. Switch it on with SHELFHOLD_COMICVINE_ENABLED=1 and give the "
                f"key of this instance in SHELFHOLD_COMICVINE_API_KEY. {NOTICE}"
            )
        if settings.comicvine_api_key is None:
            raise ExitUnavailableError(
                "comicvine needs the key of this instance: give it in SHELFHOLD_COMICVINE_API_KEY"
            )

    def _provider(self, key: str) -> MetadataProvider:
        if key in KEYED_PROVIDERS:
            with self._comp.engine.connect() as connection:
                sealed = load_provider_key(self._comp.vault(), connection, key)
            opened = sealed.reveal() if sealed is not None else None
            if key == "hardcover":
                return HardcoverProvider(token=opened)
            return GoogleBooksProvider(
                api_key=opened, daily_budget=self._comp.settings.googlebooks_daily_budget
            )
        if key != "comicvine":
            return PROVIDERS[key]()
        secret = self._comp.settings.comicvine_api_key
        if secret is not None:
            register_secret_value(secret.get_secret_value())  # the scrubbers know it before use
        return ComicVineProvider(api_key=secret)

    async def _enrich(
        self, service: EnrichService, edition_id: str, key: str, title: str | None
    ) -> EnrichReport:
        provider = self._provider(key)
        hints = (
            MatchHints(title=title.strip())
            if title and title.strip() and key in BY_TITLE | TITLE_OPTIONAL
            else None
        )
        network = self._comp.network
        runtime = provider_fetcher.default_runtime()
        runtime.buckets.use_store(SqlBucketStore(self._comp.engine))  # the balance outlives us
        fetcher = provider_fetcher.open_provider(
            provider.manifest, network.resolver, contact=self._comp.settings.metadata_contact,
            runtime=runtime, transport=network.transport,
        )  # fmt: skip
        try:
            report = await service.enrich(provider, fetcher, edition_id=edition_id, hints=hints)
            if key == "mangaupdates" and report.outcome == "enriched" and title:
                calendar = ReleaseCalendar(self._comp.engine, self._comp.clock)
                await calendar.refresh(provider, fetcher, edition_id=edition_id, title=title)
            return report
        finally:
            await fetcher.aclose()

    def release_calendar(self) -> list[dict[str, Any]]:
        """The cached release calendars, with the credit their provider asks for (operator
        reading: no profile scope, hence all of them). Nothing is requested."""
        entries = ReleaseCalendar(self._comp.engine, self._comp.clock).entries(ALL)
        return [
            {
                "edition_id": e.edition_id, "title": e.title, "provider": e.provider,
                "last_release_on": e.last_release_on.isoformat() if e.last_release_on else None,
                "chapter": e.chapter, "volume": e.volume, "attribution": e.attribution,
                "source_url": e.source_url,
            }
            for e in entries
        ]  # fmt: skip

    def check(self) -> dict[str, Any]:
        """What `rebuild` would write; nothing is."""
        try:
            report = self._projection().repair(dry_run=True)
            mirror, skipped = self._drifted_mirror_files()
        except Exception as error:
            refusal = refusal_of(error)
            if refusal is None:
                raise
            raise refusal from None
        return {
            "units": report.written, "units_read_only": report.read_only,
            "works": report.works_written, "works_read_only": report.works_read_only,
            "mirror": [name for names in mirror.values() for name in names],
            "mirror_skipped": skipped,
        }  # fmt: skip

    def rebuild(self, *, unit: str | None = None) -> dict[str, Any]:
        try:
            return self._rebuild(unit)
        except UnknownSubjectError:
            raise
        except Exception as error:
            refusal = refusal_of(error)
            if refusal is None:
                raise
            raise refusal from None

    def _rebuild(self, unit: str | None) -> dict[str, Any]:
        projection = self._projection()
        if unit is not None:
            if not is_ulid(unit) or projection.project(unit) == "missing_unit":
                raise UnknownSubjectError(f"no such Unit: {unit}")
            return {"units": [unit], "units_read_only": [], "works": [], "works_read_only": [],
                    "mirror": [], "mirror_skipped": []}  # fmt: skip
        report = projection.repair()
        mirror, skipped = self._drifted_mirror_files()
        written: list[str] = []
        if mirror:
            # a mirror file is a write to a library root: the lease, the intent, the fsops
            with self._comp.write_context() as w:
                writer = self._mirror(w.fsops)
                for destination_id in mirror:
                    written += writer.sync_destination(destination_id, writer=w.session)
                skipped = [*skipped, *writer.skipped]
        return {
            "units": report.written, "units_read_only": report.read_only,
            "works": report.works_written, "works_read_only": report.works_read_only,
            "mirror": written, "mirror_skipped": sorted(set(skipped)),
        }  # fmt: skip
