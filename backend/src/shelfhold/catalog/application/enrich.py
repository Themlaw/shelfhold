"""Enriching an Edition from a metadata provider (EF-BIB-013, ENF-LEG-005, EF-BIB-055).

The host asks one provider about one Edition through the Fetcher it opened for it (identifying
agent, token bucket, cache, robots; see `plugin_host.provider_fetcher`) and keeps what comes back
as *proposals*:

- fields go to `metadata_field` with their provenance (`propose_fields`). A locked value is never
  replaced, and neither is a hand-typed (`manual`) one, lock lifted or not: a provider fills the
  empty and replaces what another provider said, nothing an admin wrote;
- an age is stored as a suggestion (`age_suggestion`, provenance `provider`) and never as a
  rating; the removal plan of the shelves below 18+ it would now keep an Edition out of is
  returned, never applied. A field about an age is not something a provider may touch;
- the cover is a reference the provider hands over. The host downloads it (bounded) and decodes
  it in the worker (ENF-SEC-003): what is stored is what the worker re-encoded, never the bytes
  that came. If the worker refuses the image, there is no cover, and no fallback;
- a provider that is down is a notification (`metadata.provider_down`, at most once an hour) and
  nothing else: no queue pauses, nothing is retried by itself, no content error is counted.

A provider cannot put a value under another provider's name, nor under `cover` (the host's own
field); only what its manifest declares in `touched_fields` is kept. The network is spent before
the transaction opens, never inside it.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.catalog.application.external_ids import external_ids_of, holder_of
from shelfhold.catalog.application.fields import keys_of, propose_fields, read_field, suggest_age
from shelfhold.catalog.application.rating import RatingService, Withdrawal
from shelfhold.catalog.domain.external_ids import ExternalId, ExternalIdError, normalize_external_id
from shelfhold.catalog.domain.provenance import FieldKey, StoredField
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.cover_store import CoverStore
from shelfhold.infrastructure.fetcher.client import is_identifiable
from shelfhold.infrastructure.fetcher.errors import (
    DnsBlocked,
    DnsResolutionError,
    FetchError,
    FetchTimeout,
    FetchUnreachable,
    ProviderQuotaExceeded,
    RobotsDisallowed,
    RobotsUnavailable,
    UserAgentNotIdentifiable,
)
from shelfhold.infrastructure.fetcher.payload_cache import CachingFetcher, PayloadCache
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.imaging import (
    ImageKind,
    ImageRejected,
    WorkerLimits,
    caps_for,
    make_thumbnail,
)
from shelfhold.infrastructure.schema import catalog, platform
from shelfhold.sdk.age import AgeRating
from shelfhold.sdk.metadata import (
    EntityRef,
    FieldProposal,
    MatchHints,
    MetadataProvider,
    Provenance,
    ProviderCapability,
    ProviderUnavailable,
)
from shelfhold.sdk.refs import ImageRequest

__all__ = [
    "COVER_FIELD",
    "DOWN_NOTICE_INTERVAL",
    "EnrichReport",
    "EnrichService",
    "UnknownTargetError",
]

COVER_FIELD: Final = "cover"  # the host's own field: a provider never proposes it
DOWN_NOTICE_INTERVAL: Final = timedelta(hours=1)
COVER_MAX_EDGE: Final = 1200
PROVIDER_DOWN: Final = "metadata.provider_down"
_REASONS: Final = frozenset({"unreachable", "timeout", "http_503", "schema_mismatch"})
_AGE_WORDS: Final = frozenset({"age", "rating", "maturity", "x"})
_ISBN_SCHEMES: Final = (("isbn13", 13), ("isbn10", 10))


class UnknownTargetError(LookupError):
    """The argument names no Edition: not an ISBN this catalog holds, nor an Edition."""


class _Fetcher(Protocol):
    @property
    def user_agent(self) -> str: ...

    async def fetch_image(self, req: ImageRequest, *, max_bytes: int) -> bytes | None: ...


Outcome = Literal["enriched", "not_found", "provider_down", "robots_disallowed", "quota"]


@dataclass(frozen=True)
class EnrichReport:
    outcome: Outcome
    edition_id: str
    reason: str | None = None  # provider_down: unreachable, timeout, http_503...
    written: tuple[FieldKey, ...] = ()
    kept_locked: tuple[FieldKey, ...] = ()
    kept_manual: tuple[FieldKey, ...] = ()  # hand-typed, lock lifted: still never replaced
    age_suggested: tuple[AgeRating, ...] = ()
    withdrawals: tuple[Withdrawal, ...] = ()  # told, never applied
    cover: str | None = None
    cover_rejected: str | None = None  # the code of the image the worker refused
    attribution: str | None = None
    source_url: str | None = None


def is_age_field(name: str) -> bool:
    return bool(_AGE_WORDS & set(re.split(r"[^a-z0-9]+", name.lower())))


def _down_reason(error: Exception) -> str:
    if isinstance(error, ProviderUnavailable):
        return error.reason if error.reason in _REASONS else "unknown"
    return "timeout" if isinstance(error, FetchTimeout) else "unreachable"


def _age_of(data: dict[str, Any]) -> AgeRating | None:
    min_age, x = data.get("min_age"), data.get("x", False)
    if isinstance(min_age, bool) or not isinstance(min_age, int) or not isinstance(x, bool):
        return None
    try:
        return AgeRating(min_age, x)
    except ValueError:
        return None


class EnrichService:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        bus: Any,
        *,
        data_dir: Path,
        limits: WorkerLimits | None = None,
        rating: RatingService | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._bus = bus
        self._cover_store = CoverStore(data_dir)
        self._payloads = PayloadCache(engine, clock)
        self._limits = limits
        self._rating = rating or RatingService()

    # -- what is asked about ------------------------------------------------------------------
    def resolve(self, connection: Connection, target: str) -> str:
        """The Edition an ISBN (with or without hyphens) or an Edition identifier names."""
        text = target.strip()
        if is_ulid(text):
            found = connection.execute(
                sa.select(catalog.edition.c.id).where(catalog.edition.c.id == text)
            ).scalar()
            if found is None:
                raise UnknownTargetError("no such Edition")
            return str(found)
        try:
            isbn = normalize_external_id("edition", "isbn", text).value
        except ExternalIdError:
            raise UnknownTargetError("not an Edition identifier nor a valid ISBN") from None
        holder = holder_of(connection, ExternalId("edition", "isbn", isbn))
        if holder is None:
            of_volume = holder_of(connection, ExternalId("volume", "isbn", isbn))
            holder = of_volume and self._edition_of_volume(connection, of_volume)
        if holder is None:
            holder = connection.execute(
                sa.select(catalog.volume.c.edition_id).where(catalog.volume.c.isbn == isbn)
            ).scalar()
        if holder is None:
            raise UnknownTargetError("no Edition of this catalog has this ISBN")
        return str(holder)

    @staticmethod
    def _edition_of_volume(connection: Connection, volume_id: str) -> str | None:
        found = connection.execute(
            sa.select(catalog.volume.c.edition_id).where(catalog.volume.c.id == volume_id)
        ).scalar()
        return None if found is None else str(found)

    def _ids_of(self, connection: Connection, edition_id: str) -> dict[str, str]:
        ids: dict[str, str] = {}
        for stored in external_ids_of(connection, "edition", edition_id):
            ext = stored.external
            if stored.state != "current":
                continue
            if ext.provider == "isbn":
                ids["isbn13" if len(ext.value) == 13 else "isbn10"] = ext.value
            elif ext.provider == "openlibrary":
                ids["openlibrary"] = ext.value
        if "isbn13" not in ids:
            volume = connection.execute(
                sa.select(catalog.volume.c.isbn)
                .where(
                    catalog.volume.c.edition_id == edition_id, catalog.volume.c.isbn.is_not(None)
                )
                .order_by(catalog.volume.c.number_sort, catalog.volume.c.id)
            ).scalar()
            if volume:
                ids["isbn13"] = str(volume)
        if not ids:
            raise UnknownTargetError(
                "this Edition has no ISBN nor OpenLibrary identifier to ask by"
            )
        return ids

    # -- the enrichment -----------------------------------------------------------------------
    async def enrich(
        self, provider: MetadataProvider, fetcher: _Fetcher, *, edition_id: str
    ) -> EnrichReport:
        if not is_identifiable(getattr(fetcher, "user_agent", "")):
            raise UserAgentNotIdentifiable("a provider is only called with an identifying agent")
        fetcher = CachingFetcher(fetcher, self._payloads, provider.manifest.key)
        with self._engine.connect() as connection:
            ids = self._ids_of(connection, edition_id)
            cover_open = self._cover_open(connection, edition_id)
        target = EntityRef(entity_type="edition", ids=ids, entity_id=edition_id)
        manifest = provider.manifest
        try:
            result = await provider.identify(fetcher, target, MatchHints())
        except RobotsUnavailable:  # the site's robots.txt cannot be read: its outage, not its no
            return self._down(edition_id, "unreachable")
        except RobotsDisallowed:
            return EnrichReport("robots_disallowed", edition_id)
        except ProviderQuotaExceeded:
            return EnrichReport("quota", edition_id)
        except (ProviderUnavailable, FetchUnreachable, FetchTimeout, DnsResolutionError,
                DnsBlocked) as error:  # fmt: skip
            return self._down(edition_id, _down_reason(error))
        proposals = [p for p in result.proposals if self._acceptable(p, provider)]
        if not proposals and not any(r.kind == "age_suggestion" for r in result.relations):
            return EnrichReport("not_found", edition_id)  # nothing known: no cover is asked for
        cover_ref, rejected, cover_source = None, None, None
        if cover_open and ProviderCapability.COVER in manifest.capabilities:
            cover_ref, rejected, cover_source = await self._cover(provider, fetcher, target)
        now = self._clock.now()
        if cover_ref is not None:
            proposals.append(self._cover_proposal(provider, cover_ref, cover_source, now))
        ages = [
            a for r in result.relations
            if r.kind == "age_suggestion" and r.subject.entity_type == "edition"
            and r.provenance.provider == manifest.key and (a := _age_of(r.data)) is not None
        ]  # fmt: skip
        with self._engine.begin() as connection:
            keep, manual = self._split_manual(connection, edition_id, proposals)
            report = propose_fields(connection, {"edition": edition_id}, keep, now=now)
            withdrawals: list[Withdrawal] = []
            for age in ages:
                withdrawals += suggest_age(
                    connection, "edition", edition_id, age, provenance="provider", now=now,
                    rating=self._rating,
                )  # fmt: skip
        found = bool(proposals or ages)
        first = proposals[0].provenance if proposals else None
        return EnrichReport(
            "enriched" if found else "not_found",
            edition_id,
            written=report.written,
            kept_locked=report.kept_locked,
            kept_manual=manual,
            age_suggested=tuple(ages),
            withdrawals=tuple(dict.fromkeys(withdrawals)),
            cover=cover_ref if cover_ref is not None and key_written(report.written) else None,
            cover_rejected=rejected,
            attribution=(first.attribution if first else None) or manifest.attribution,
            source_url=next((p.provenance.source_url for p in proposals
                             if p.provenance.source_url), None),
        )  # fmt: skip

    @staticmethod
    def _acceptable(p: FieldProposal, provider: MetadataProvider) -> bool:
        """Only what the manifest declares, about the Edition asked, under the provider's own
        name; never the host's cover, never anything about an age."""
        manifest = provider.manifest
        return (
            p.field in manifest.touched_fields
            and p.field != COVER_FIELD
            and not is_age_field(p.field)
            and p.entity_type == "edition"
            and p.provenance.provider == manifest.key
        )

    @staticmethod
    def _cover_proposal(
        provider: MetadataProvider, ref: str, source: str | None, now: datetime
    ) -> FieldProposal:
        manifest = provider.manifest
        return FieldProposal(
            entity_type="edition",
            field=COVER_FIELD,
            value=ref,
            confidence=0.8,
            provenance=Provenance(
                provider=manifest.key, fetched_at=now, source_url=source,
                attribution=manifest.attribution,
            ),
        )  # fmt: skip

    def _cover_open(self, connection: Connection, edition_id: str) -> bool:
        """False when the Edition's cover is locked or typed by hand: not even asked for."""
        stored = read_field(connection, FieldKey("edition", edition_id, COVER_FIELD))
        return stored is None or not (stored.locked or stored.provenance == "manual")

    @staticmethod
    def _split_manual(
        connection: Connection, edition_id: str, proposals: list[FieldProposal]
    ) -> tuple[list[FieldProposal], tuple[FieldKey, ...]]:
        keep: list[FieldProposal] = []
        manual: list[FieldKey] = []
        for proposal, key in zip(
            proposals, keys_of(proposals, {"edition": edition_id}), strict=True
        ):
            stored: StoredField | None = read_field(connection, key)
            if stored is not None and stored.provenance == "manual" and not stored.locked:
                manual.append(key)
            else:
                keep.append(proposal)
        return keep, tuple(manual)

    # -- the cover, through the worker ------------------------------------------------------
    async def _cover(
        self, provider: MetadataProvider, fetcher: _Fetcher, target: EntityRef
    ) -> tuple[str | None, str | None, str | None]:
        """(reference, code of the refusal, address): the reference of a cover the worker decoded
        and re-encoded, or why there is none. Nothing the worker did not produce is kept."""
        try:
            request = await provider.get_cover(fetcher, target)  # type: ignore[arg-type]
            if request is None:
                return None, None, None
            caps = caps_for(ImageKind.COVER)
            data = await fetcher.fetch_image(request, max_bytes=caps.max_bytes)
        except (FetchError, ProviderUnavailable):
            return None, None, None  # no cover this time: the text is still worth keeping
        if data is None:
            return None, None, None
        try:
            thumbnail = await asyncio.to_thread(
                make_thumbnail, data, ImageKind.COVER, max_edge=COVER_MAX_EDGE, fmt="webp",
                limits=self._limits,
            )  # fmt: skip
        except ImageRejected as refused:
            return None, refused.code, None
        return self._cover_store.put(thumbnail.data), None, request.url.split("?", 1)[0]

    # -- a provider that is down -------------------------------------------------------------
    def _down(self, edition_id: str, reason: str) -> EnrichReport:
        """Tell the admin once an hour; change nothing else."""
        now = self._clock.now()
        with self._engine.begin() as connection:
            recent = connection.execute(
                sa.select(sa.func.count())
                .select_from(platform.event)
                .where(
                    platform.event.c.type == PROVIDER_DOWN,
                    platform.event.c.occurred_at > now - DOWN_NOTICE_INTERVAL,
                )
            ).scalar_one()
            if not recent:
                self._bus.publish(connection, PROVIDER_DOWN, {"reason": reason})
        return EnrichReport("provider_down", edition_id, reason=reason)


def key_written(written: tuple[FieldKey, ...]) -> bool:
    return any(k.field == COVER_FIELD for k in written)
