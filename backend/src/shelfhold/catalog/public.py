"""The catalog facade: the only door other contexts use (import-linter `contexts-independent`)."""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa

from shelfhold.catalog.adapters.repo import (
    SqlContributorStore,
    SqlEditionStore,
    SqlTeamStore,
    SqlUnitStore,
    SqlWorkStore,
)
from shelfhold.catalog.adapters.rights_store import SqlRightsStore
from shelfhold.catalog.adapters.tx import run_immediate
from shelfhold.catalog.application.auto_attach import (
    ATTACH_SUGGESTED,
    AUTO_ATTACHED,
    Arrival,
    AttachPlan,
    AttachPlanStaleError,
    AttachRefusedError,
    AttachView,
    AutoAttachService,
    Detached,
    EventPublisher,
    IncomingEdition,
    Refiling,
)
from shelfhold.catalog.application.changes import (
    ADMIN_SCOPE,
    MAX_REMOVED,
    NEED_NONE,
    TooManyRemovalsError,
    history_marker,
    record_removed,
    record_work_removed,
    removed_ids,
    restore_epoch,
    scope_code,
    track_work_changes,
    window,
    work_need,
)
from shelfhold.catalog.application.contributors import ARTIST, WRITER
from shelfhold.catalog.application.coverage import (
    Bound,
    ConfirmationRequiredError,
    CoverageMismatchError,
    CoverageReport,
    ResplitError,
    ResplitProposal,
    ResplitSigner,
    apply_resplit,
    bounds_from_toc,
    check_coverage,
    propose_resplit,
    stored_bounds,
    verify_item,
)
from shelfhold.catalog.application.dating import period_start, stored_date
from shelfhold.catalog.application.dedup import (
    open_offset_suggestion,
    open_suggestions,
    suggest_duplicates,
)
from shelfhold.catalog.application.dedup import tombstoned as tombstoned_work_ids
from shelfhold.catalog.application.dto import (
    BindingRef,
    EditionRef,
    EditionSpec,
    IncomingUnit,
    IngestResult,
    TeamRef,
)
from shelfhold.catalog.application.editions import translation_type_for
from shelfhold.catalog.application.enrich import (
    COVER_FIELD,
    EnrichReport,
    EnrichService,
    UnknownTargetError,
)
from shelfhold.catalog.application.external_ids import (
    ExternalIdConflictError,
    StoredExternalId,
    add_external_id,
    external_ids_of,
    holder_of,
    renumber_external_id,
)
from shelfhold.catalog.application.fields import (
    AgeFieldError,
    FieldReport,
    edit_field,
    lock_current,
    lock_field,
    propose_fields,
    suggest_age,
    unlock_field,
)
from shelfhold.catalog.application.ingest import UnsupportedFileError, announced_kind
from shelfhold.catalog.application.merge import (
    EDITION_KEY,
    EditionMerged,
    EditionMergePlan,
    EditionMergeRefusedError,
    StaleMergePlanError,
    apply_edition_merge,
    plan_after_contributor_merge,
    plan_edition_merge,
)
from shelfhold.catalog.application.new_edition_gate import Filing, file_new_edition
from shelfhold.catalog.application.places import place_key
from shelfhold.catalog.application.ports import StoredVariant
from shelfhold.catalog.application.queries import (
    ReaderScope,
    VisibleWork,
    count_visible_works,
    cover_ref_visible,
    list_visible_series,
    list_visible_works,
    referenced_cover_refs,
    visible_edition_ids,
    visible_editions_clause,
    visible_series_ids,
    visible_unit_ids,
    visible_work_ids,
    visible_works_clause,
)
from shelfhold.catalog.application.rating import (
    EditionSuggestion,
    RatingRefusedError,
    RatingResult,
    RatingService,
    Withdrawal,
)
from shelfhold.catalog.application.relations import (
    RelatedWork,
    RelationError,
    relate_works,
    relations_of,
    unrelate_works,
)
from shelfhold.catalog.application.relink import (
    ListedUnit,
    Relinker,
    RelinkRefusedError,
    RelinkRequest,
    RelinkResult,
    detect_suwayomi_migrations,
)
from shelfhold.catalog.application.revisions import (
    ApplyReport,
    ConfirmationError,
    ForceRefusedError,
    PendingRevision,
    RevisionQueue,
    RevisionReport,
    RewriteRequester,
    RewriteUnavailableError,
)
from shelfhold.catalog.application.rights import Rights, RightsReport
from shelfhold.catalog.application.search import (
    SearchCapability,
    SearchHit,
    SearchPage,
    index_work,
    search_capability,
    search_page,
)
from shelfhold.catalog.application.search import (
    ensure_tables as ensure_search_tables,
)
from shelfhold.catalog.application.search import rebuild as _rebuild_search
from shelfhold.catalog.application.search import remove_works as remove_from_search_index
from shelfhold.catalog.application.search import sync as sync_search_index
from shelfhold.catalog.application.separate import (
    MergeAlreadyUndoneError,
    MergeExpiredError,
    Separated,
    SeparationBlockedError,
    separate_works,
)
from shelfhold.catalog.application.series import SeriesAlreadyAttachedError
from shelfhold.catalog.application.service import CatalogService
from shelfhold.catalog.application.tombstone_screen import (
    MergedPlace,
    TombstoneScreen,
    TombstoneScreenMissingError,
)
from shelfhold.catalog.application.work_merge import (
    MERGE_WINDOW_DAYS,
    WorkMerged,
    WorkMergePlan,
    WorkMergeRefusedError,
    apply_work_merge,
    plan_work_merge,
    resolve_work,
)
from shelfhold.catalog.domain.age import (
    Admission,
    EditionAge,
    admission,
    ceiling_of,
    content_warning_age,
    effective_age,
    parse_age,
)
from shelfhold.catalog.domain.dates import parse_partial_date
from shelfhold.catalog.domain.external_ids import (
    ExternalId,
    ExternalIdError,
    normalize_external_id,
)
from shelfhold.catalog.domain.identity import (
    BOOK_KINDS,
    TEXT_VERSION_LABELS,
    Discriminant,
    TextVersionRef,
    translation_discriminant,
)
from shelfhold.catalog.domain.names import (
    ContributorName,
    NameKind,
    NoPersonFieldName,
    PersonField,
    join_people,
    person_field_name,
)
from shelfhold.catalog.domain.normalizers import (
    NormalizerId,
    NormalizerRegistry,
    default_registry,
)
from shelfhold.catalog.domain.numbering import NumberError, parse_number
from shelfhold.catalog.domain.placement import IngestOutcome, VariantFacts, place_of
from shelfhold.catalog.domain.provenance import FieldKey, is_age_field
from shelfhold.catalog.domain.public_domain import Verdict
from shelfhold.catalog.domain.revisions import DEFAULT_MIN_PAGE_RATIO, suspect_reason
from shelfhold.catalog.domain.sort_key import sort_key
from shelfhold.catalog.domain.subject_filter import (
    MAX_TERM_CHARS,
    MAX_TERMS,
    InvalidTermError,
    check_terms,
    clean_terms,
    is_flagged,
    visible_title,
)
from shelfhold.catalog.domain.team_offset import (
    OffsetVerdict,
    TeamFacts,
    check_before_fallback,
)
from shelfhold.catalog.lang import LANG_NORMALIZER_ID, LanguageError, edition_key_lang
from shelfhold.events.bus import EventBus
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.schema import catalog as catalog_schema
from shelfhold.infrastructure.schema import storage as storage_schema


class Catalog(CatalogService):
    """The catalog on an open connection; the caller owns the transaction."""

    def __init__(self, connection: sa.Connection) -> None:
        super().__init__(
            SqlWorkStore(connection),
            SqlEditionStore(connection),
            SqlTeamStore(connection),
            SqlUnitStore(connection),
            SqlContributorStore(connection),
        )


class _Bus(Protocol):
    def publish(
        self, connection: sa.Connection, event_type: str, payload: Mapping[str, object] | None = ...
    ) -> object: ...


class RightsService(Rights):
    """The rights review over an open connection; `bus` announces the warning in the caller's
    transaction (EF-SRC-024). The warning never blocks: nothing here raises over what it finds."""

    def __init__(self, connection: sa.Connection, bus: _Bus | None = None) -> None:
        publish: Callable[[str, Mapping[str, object]], object] | None = None
        if bus is not None:
            announcer = bus

            def publish(event_type: str, payload: Mapping[str, object]) -> object:
                return announcer.publish(connection, event_type, payload)

        super().__init__(SqlRightsStore(connection), publish)


class RelinkService(Relinker):
    """`relink` over the catalog of an open connection (EF-BIB-029)."""

    def __init__(self, rating: RatingService | None = None) -> None:
        super().__init__(Catalog, rating or RatingService())


def relink_isolated(
    engine: sa.Engine, request: RelinkRequest, *, principal: Principal, now: datetime
) -> RelinkResult:
    """`relink` in its own `BEGIN IMMEDIATE` transaction, audit entry included."""
    return run_immediate(
        engine, lambda c: RelinkService().relink(c, request, principal=principal, now=now)
    )


def ingest_isolated(
    engine: sa.Engine,
    item: IncomingUnit,
    *,
    then: Callable[[sa.Connection, IngestResult], None] | None = None,
    min_page_ratio: float = DEFAULT_MIN_PAGE_RATIO,
) -> IngestResult:
    """Ingest one release in its own `BEGIN IMMEDIATE` transaction, replayed once on a clash.

    `then` runs in the same transaction, after the ingest: what must not exist without the
    Variant (a download ticket) commits or rolls back with it. It must be idempotent (a replay).
    """

    def work(connection: sa.Connection) -> IngestResult:
        result = Catalog(connection).ingest(item, min_page_ratio=min_page_ratio)
        if then is not None:
            then(connection, result)
        return result

    return run_immediate(engine, work)


def guard_received(
    connection: sa.Connection,
    *,
    variant_id: str,
    pages: int,
    content_hash: str | None,
    seen_at: datetime,
    min_page_ratio: float = DEFAULT_MIN_PAGE_RATIO,
) -> str | None:
    """The suspect-revision guard at reception (EF-BIB-024): a file received for a Variant that
    already has one is compared with the page count of the stored version, whatever the source
    said. Call it **before** the new content is recorded. A file under the ratio is recorded as a
    quarantined revision (warning, queued as a rewrite): the caller keeps the received file, but
    nothing promotes it. Returns the reason, or None."""
    store = SqlUnitStore(connection)
    v, r, c = (
        catalog_schema.unit_variant.c,
        catalog_schema.variant_revision.c,
        storage_schema.content.c,
    )
    row = connection.execute(
        sa.select(v.id, v.unit_id, v.team_id, v.revision).where(v.id == variant_id)
    ).first()
    if row is None:
        return None
    variant = StoredVariant(*row)
    baseline = store.stored_pages(variant)
    if baseline is None:  # another Variant of the Unit is the one stored and exported
        baseline = connection.execute(
            sa.select(c.page_count)
            .where(c.unit_id == variant.unit_id, c.page_count.is_not(None))
            .order_by(c.created_at.asc(), c.id.asc())
            .limit(1)
        ).scalar()
    reason = suspect_reason(
        stored_pages=baseline, new_pages=pages, availability="free", min_ratio=min_page_ratio
    )
    if reason is None:
        return None
    latest = connection.execute(
        sa.select(r.id, r.revision, r.page_count, r.quarantined_at)
        .where(r.variant_id == variant_id)
        .order_by(r.revision.desc())
        .limit(1)
    ).first()
    if latest is not None and latest.quarantined_at is None and latest.page_count is None:
        # the revision recorded when the Variant was listed, never given a count: it gets the mark
        # (the one column the history lets change, once); its own count stays unknown
        connection.execute(
            sa.update(catalog_schema.variant_revision)
            .where(r.id == latest.id)
            .values(quarantined_at=seen_at, quarantine_reason=reason)
        )
        revision = int(latest.revision)
        EventBus(SystemClock()).publish(
            connection, "unit.revision_suspect",
            {"unit_id": variant.unit_id, "variant_id": variant_id, "reason": reason},
        )  # fmt: skip
    else:
        item = IncomingUnit(
            binding=BindingRef("", ""), remote_unit_id="", raw_number=None, seen_at=seen_at,
            page_count=pages, content_hash=content_hash,
        )  # fmt: skip
        revision = store.add_revision(variant, item, quarantine=reason)
    store.queue_revision(variant, revision, nature="rewrite", field=None, value=None)
    return reason


def place_parts(raw_number: str | None, source_order: int = 0) -> tuple[str, str, str]:
    """(track, segment, part) of the place a release takes, as the ingest would file it: what a
    `place` tombstone is compared with (EF-BIB-053)."""
    try:
        number = parse_number(raw_number, source_order=source_order)
    except NumberError:
        number = parse_number(None, source_order=source_order)
    place = place_of(number)
    return place.track, place.segment, place.part


def numbered_mark(raw_number: str | None, source_order: int = 0) -> tuple[str, str] | None:
    """`(segment, number_norm)` of a numbered release as the ingest would file it, `None` when it
    is unnumbered: what a follow watermark is made of (EF-EXP-053)."""
    try:
        number = parse_number(raw_number, source_order=source_order)
    except NumberError:
        return None
    place = place_of(number)
    return None if place.unnumbered else (place.segment, place.number_norm)


def rebuild_search_index(connection: sa.Connection) -> int:
    """The search index again, from the database alone; the number of Works indexed."""
    return _rebuild_search(connection)[0]


__all__ = [
    "ADMIN_SCOPE",
    "ARTIST",
    "ATTACH_SUGGESTED",
    "AUTO_ATTACHED",
    "BOOK_KINDS",
    "COVER_FIELD",
    "DEFAULT_MIN_PAGE_RATIO",
    "EDITION_KEY",
    "LANG_NORMALIZER_ID",
    "MAX_REMOVED",
    "MAX_TERMS",
    "MAX_TERM_CHARS",
    "MERGE_WINDOW_DAYS",
    "NEED_NONE",
    "TEXT_VERSION_LABELS",
    "WRITER",
    "Admission",
    "AgeFieldError",
    "ApplyReport",
    "Arrival",
    "AttachPlan",
    "AttachPlanStaleError",
    "AttachRefusedError",
    "AttachView",
    "AutoAttachService",
    "BindingRef",
    "Bound",
    "Catalog",
    "ConfirmationError",
    "ConfirmationRequiredError",
    "ContributorName",
    "CoverageMismatchError",
    "CoverageReport",
    "Detached",
    "Discriminant",
    "EditionAge",
    "EditionMergePlan",
    "EditionMergeRefusedError",
    "EditionMerged",
    "EditionRef",
    "EditionSpec",
    "EditionSuggestion",
    "EnrichReport",
    "EnrichService",
    "EventPublisher",
    "ExternalId",
    "ExternalIdConflictError",
    "ExternalIdError",
    "FieldKey",
    "FieldReport",
    "Filing",
    "ForceRefusedError",
    "IncomingEdition",
    "IncomingUnit",
    "IngestOutcome",
    "IngestResult",
    "InvalidTermError",
    "LanguageError",
    "ListedUnit",
    "MergeAlreadyUndoneError",
    "MergeExpiredError",
    "MergedPlace",
    "NameKind",
    "NoPersonFieldName",
    "NormalizerId",
    "NormalizerRegistry",
    "OffsetVerdict",
    "PendingRevision",
    "PersonField",
    "RatingRefusedError",
    "RatingResult",
    "RatingService",
    "ReaderScope",
    "Refiling",
    "RelatedWork",
    "RelationError",
    "RelinkRefusedError",
    "RelinkRequest",
    "RelinkResult",
    "RelinkService",
    "ResplitError",
    "ResplitProposal",
    "ResplitSigner",
    "RevisionQueue",
    "RevisionReport",
    "RewriteRequester",
    "RewriteUnavailableError",
    "RightsReport",
    "RightsService",
    "SearchCapability",
    "SearchHit",
    "SearchPage",
    "Separated",
    "SeparationBlockedError",
    "SeriesAlreadyAttachedError",
    "StaleMergePlanError",
    "StoredExternalId",
    "TeamFacts",
    "TeamRef",
    "TextVersionRef",
    "TombstoneScreen",
    "TombstoneScreenMissingError",
    "TooManyRemovalsError",
    "UnknownTargetError",
    "UnsupportedFileError",
    "VariantFacts",
    "Verdict",
    "VisibleWork",
    "Withdrawal",
    "WorkMergePlan",
    "WorkMergeRefusedError",
    "WorkMerged",
    "add_external_id",
    "admission",
    "announced_kind",
    "apply_edition_merge",
    "apply_resplit",
    "apply_work_merge",
    "bounds_from_toc",
    "ceiling_of",
    "check_before_fallback",
    "check_coverage",
    "check_terms",
    "clean_terms",
    "content_warning_age",
    "count_visible_works",
    "cover_ref_visible",
    "default_registry",
    "detect_suwayomi_migrations",
    "edit_field",
    "edition_key_lang",
    "effective_age",
    "ensure_search_tables",
    "external_ids_of",
    "file_new_edition",
    "guard_received",
    "history_marker",
    "holder_of",
    "index_work",
    "ingest_isolated",
    "is_age_field",
    "is_flagged",
    "join_people",
    "list_visible_series",
    "list_visible_works",
    "lock_current",
    "lock_field",
    "normalize_external_id",
    "numbered_mark",
    "open_offset_suggestion",
    "open_suggestions",
    "parse_age",
    "parse_number",
    "parse_partial_date",
    "period_start",
    "person_field_name",
    "place_key",
    "place_parts",
    "plan_after_contributor_merge",
    "plan_edition_merge",
    "plan_work_merge",
    "propose_fields",
    "propose_resplit",
    "rebuild_search_index",
    "record_removed",
    "record_work_removed",
    "referenced_cover_refs",
    "relate_works",
    "relations_of",
    "relink_isolated",
    "remove_from_search_index",
    "removed_ids",
    "renumber_external_id",
    "resolve_work",
    "restore_epoch",
    "scope_code",
    "search_capability",
    "search_page",
    "separate_works",
    "sort_key",
    "stored_bounds",
    "stored_date",
    "suggest_age",
    "suggest_duplicates",
    "suspect_reason",
    "sync_search_index",
    "tombstoned_work_ids",
    "track_work_changes",
    "translation_discriminant",
    "translation_type_for",
    "unlock_field",
    "unrelate_works",
    "verify_item",
    "visible_edition_ids",
    "visible_editions_clause",
    "visible_series_ids",
    "visible_title",
    "visible_unit_ids",
    "visible_work_ids",
    "visible_works_clause",
    "window",
    "work_need",
]
