"""SQLAlchemy Core repositories of the catalog (the only place that writes its tables).

All of them take an open `Connection`: the caller owns the transaction. Nothing here decides
anything; the rules are in `catalog.domain` and `catalog.application`.
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from shelfhold.catalog.application.dto import (
    EditionRef,
    EditionSpec,
    IncomingUnit,
    UnitView,
)
from shelfhold.catalog.application.ports import StoredVariant
from shelfhold.catalog.domain.identity import sort_title
from shelfhold.catalog.domain.normalizers import Normalized
from shelfhold.catalog.domain.numbering import NUMBER_NORMALIZER_ID, UnitNumber
from shelfhold.catalog.domain.placement import NUMBERING_CONFLICT, Place, VariantFacts
from shelfhold.catalog.lang import variant_lang
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog

_TRACK_RANK = {track: rank for rank, track in enumerate(catalog.UNIT_TRACKS)}


class SqlWorkStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection

    def insert_series(self, title: str) -> str:
        series_id = new_ulid()
        self._c.execute(
            sa.insert(catalog.series).values(
                id=series_id, title=title, sort_title=sort_title(title)
            )
        )
        return series_id

    def insert_work(self, kind: str, title: str, series_id: str | None, position: str) -> str:
        work_id = new_ulid()
        self._c.execute(
            sa.insert(catalog.work).values(
                id=work_id, kind=kind, title=title, sort_title=sort_title(title)
            )
        )
        if series_id is not None:
            self._c.execute(
                sa.insert(catalog.series_member).values(
                    series_id=series_id, work_id=work_id, position=position
                )
            )
        return work_id

    def series_of(self, work_id: str) -> str | None:
        return self._c.execute(
            sa.select(catalog.series_member.c.series_id).where(
                catalog.series_member.c.work_id == work_id
            )
        ).scalar()

    def insert_contributor(self, name: str) -> str:
        contributor_id = new_ulid()
        self._c.execute(sa.insert(catalog.contributor).values(id=contributor_id, primary_name=name))
        return contributor_id


class SqlEditionStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection

    def find(self, spec: EditionSpec, lang: str, text_version: str) -> str | None:
        e = catalog.edition.c
        return self._c.execute(
            sa.select(e.id).where(
                e.work_id == spec.work_id,
                e.lang == lang,
                e.translation_type == spec.translation_type,
                e.media == spec.media,
                e.publisher == spec.publisher,
                e.text_version == text_version,
                e.performance == spec.performance,
                e.packaging == spec.packaging,
            )
        ).scalar()

    def insert(self, spec: EditionSpec, lang: str, text_version: str) -> EditionRef:
        edition_id = new_ulid()
        ref = spec.text_version
        self._c.execute(
            sa.insert(catalog.edition).values(
                id=edition_id,
                work_id=spec.work_id,
                lang=lang,
                lang_raw=spec.lang,
                translation_type=spec.translation_type,
                media=spec.media,
                publisher=spec.publisher,
                text_version=text_version,
                text_version_contributor_id=ref.contributor_id if ref else None,
                text_version_label=ref.label if ref else None,
                text_version_norm_by=ref.normalized_by if ref else None,
                performance=spec.performance,
                packaging=spec.packaging,
                is_official=spec.translation_type == "official",
            )
        )
        return EditionRef(edition_id, spec.work_id, text_version, created=True)


class SqlTeamStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection
        self._editions: dict[str, str] = {}

    def binding_edition(self, binding_id: str) -> str | None:
        """A binding never changes Edition: the answer is kept for the life of this store."""
        if binding_id not in self._editions:
            b = acquisition.source_binding.c
            found = self._c.execute(sa.select(b.edition_id).where(b.id == binding_id)).scalar()
            if found is None:
                return None
            self._editions[binding_id] = str(found)
        return self._editions[binding_id]

    def find(self, edition_id: str, label: str) -> str | None:
        t = catalog.team.c
        return self._c.execute(
            sa.select(t.id).where(t.edition_id == edition_id, t.label == label)
        ).scalar()

    def insert(self, edition_id: str, normalized: Normalized) -> str:
        team_id = new_ulid()
        self._c.execute(
            sa.insert(catalog.team).values(
                id=team_id,
                edition_id=edition_id,
                label=normalized.value,
                label_raw=normalized.raw,
                label_norm_by=normalized.by,
            )
        )
        return team_id

    def add_alias(self, team_id: str, binding_id: str, raw: str, seen_at: datetime) -> None:
        a = catalog.team_alias.c
        known = self._c.execute(
            sa.select(sa.literal(1)).where(
                a.team_id == team_id, a.source_binding_id == binding_id, a.label == raw
            )
        ).first()
        if known is None:
            self._c.execute(
                sa.insert(catalog.team_alias).values(
                    team_id=team_id, source_binding_id=binding_id, label=raw, first_seen_at=seen_at
                )
            )

    def priority(self, edition_id: str) -> list[str]:
        p = catalog.team_priority.c
        return list(
            self._c.execute(
                sa.select(p.team_id).where(p.edition_id == edition_id).order_by(p.rank)
            ).scalars()
        )

    def set_priority(self, edition_id: str, team_ids: list[str]) -> None:
        """Rewrite the list; a Team of another Edition is refused by the composite foreign key."""
        p = catalog.team_priority
        self._c.execute(sa.delete(p).where(p.c.edition_id == edition_id))
        if team_ids:
            self._c.execute(
                sa.insert(p),
                [
                    {"edition_id": edition_id, "team_id": team_id, "rank": rank}
                    for rank, team_id in enumerate(team_ids, start=1)
                ],
            )


class SqlUnitStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection

    def find_variant(self, binding_id: str, remote_unit_id: str) -> StoredVariant | None:
        v = catalog.unit_variant.c
        row = self._c.execute(
            sa.select(v.id, v.unit_id, v.team_id, v.revision).where(
                v.source_binding_id == binding_id, v.remote_unit_id == remote_unit_id
            )
        ).first()
        return None if row is None else StoredVariant(*row)

    def _last_revision(self, variant: StoredVariant) -> tuple[str | None, str | None, int | None]:
        r = catalog.variant_revision.c
        row = self._c.execute(
            sa.select(r.revision_hint, r.content_hash, r.page_count).where(
                r.variant_id == variant.id, r.revision == variant.revision
            )
        ).first()
        return (None, None, None) if row is None else (row[0], row[1], row[2])

    def revision_changed(self, variant: StoredVariant, item: IncomingUnit) -> bool:
        """Only the fields the source gave count: an absent field (`None`) is not a change."""
        last = self._last_revision(variant)
        seen = (item.revision_hint, item.content_hash, item.page_count)
        return any(new is not None and new != old for new, old in zip(seen, last, strict=True))

    def add_revision(self, variant: StoredVariant, item: IncomingUnit) -> int:
        """Append-only: the history gains a row, the Variant's identity is not touched.

        A field the source left out keeps its previous value in the new row, so the next
        comparison does not read the gap as a change.
        """
        revision = variant.revision + 1
        last = self._last_revision(variant)
        hint, content_hash, pages = (
            (new if new is not None else old)
            for new, old in zip(
                (item.revision_hint, item.content_hash, item.page_count),
                last,
                strict=True,
            )
        )
        self._c.execute(
            sa.insert(catalog.variant_revision).values(
                variant_id=variant.id,
                revision=revision,
                seen_at=item.seen_at,
                page_count=pages,
                content_hash=content_hash,
                revision_hint=hint,
            )
        )
        v = catalog.unit_variant
        self._c.execute(
            sa.update(v).where(v.c.id == variant.id).values(revision=revision, revision_hint=hint)
        )
        return revision

    def reappear(self, variant_id: str) -> None:
        v = catalog.unit_variant
        self._c.execute(
            sa.update(v)
            .where(v.c.id == variant_id, v.c.removed_at.is_not(None))
            .values(removed_at=None)
        )

    def find_unit(self, edition_id: str, place: Place) -> str | None:
        u = catalog.unit.c
        if place.unnumbered:
            where = [u.track == place.track, u.source_order == place.source_order]
        else:
            where = [
                u.track == place.track,
                u.number_segment == place.segment,
                u.number_norm == place.number_norm,
                u.part == place.part,
            ]
        return self._c.execute(sa.select(u.id).where(u.edition_id == edition_id, *where)).scalar()

    def insert_unit(
        self, edition_id: str, number: UnitNumber, place: Place, item: IncomingUnit
    ) -> str:
        unit_id = new_ulid()
        self._c.execute(
            sa.insert(catalog.unit).values(
                id=unit_id,
                edition_id=edition_id,
                number_raw=number.raw,
                number_norm=place.number_norm,
                number_norm_by=NUMBER_NORMALIZER_ID,
                number_sort=float(number.sort) if place.number_norm else None,
                number_segment=place.segment,
                number_segment_norm_by=NUMBER_NORMALIZER_ID if place.segment else None,
                number_segment_ordinal=number.segment_ordinal,
                part=place.part,
                track=place.track,
                source_order=number.source_order,
                kind=item.kind,
                title=item.title,
                published_at=item.published_at,
            )
        )
        return unit_id

    def facts_at(self, unit_id: str) -> list[VariantFacts]:
        v, p, t = catalog.unit_variant.c, catalog.team_priority.c, catalog.unit.c
        rows = self._c.execute(
            sa.select(v.id, v.source_binding_id, v.team_id, v.first_seen_at, v.removed_at, p.rank)
            .select_from(
                catalog.unit_variant.join(catalog.unit, t.id == v.unit_id).outerjoin(
                    catalog.team_priority,
                    sa.and_(p.team_id == v.team_id, p.edition_id == t.edition_id),
                )
            )
            .where(v.unit_id == unit_id)
        )
        return [
            VariantFacts(
                r.id,
                r.source_binding_id,
                r.team_id,
                r.first_seen_at,
                r.removed_at is not None,
                r.rank,
            )
            for r in rows
        ]

    def insert_variant(
        self, unit_id: str, team_id: str | None, supersedes: str | None, item: IncomingUnit
    ) -> str:
        variant_id = new_ulid()
        lang = variant_lang(None, item.lang)
        self._c.execute(
            sa.insert(catalog.unit_variant).values(
                id=variant_id,
                unit_id=unit_id,
                source_binding_id=item.binding.id,
                remote_unit_id=item.remote_unit_id,
                team_id=team_id,
                lang=lang or "und",
                lang_raw=item.lang,
                revision=1,
                revision_hint=item.revision_hint,
                supersedes_variant_id=supersedes,
                first_seen_at=item.seen_at,
            )
        )
        self._c.execute(
            sa.insert(catalog.variant_revision).values(
                variant_id=variant_id,
                revision=1,
                seen_at=item.seen_at,
                page_count=item.page_count,
                content_hash=item.content_hash,
                revision_hint=item.revision_hint,
            )
        )
        return variant_id

    def open_conflict(self, first: str, second: str) -> str:
        """One open suggestion per unordered pair (the table keeps `a < b`)."""
        a, b = sorted((first, second))
        m = catalog.merge_suggestion
        existing = self._c.execute(
            sa.select(m.c.id).where(
                m.c.entity_type == "variant",
                m.c.a == a,
                m.c.b == b,
                m.c.reason == NUMBERING_CONFLICT,
            )
        ).scalar()
        if existing is not None:
            return str(existing)
        suggestion_id = new_ulid()
        self._c.execute(
            sa.insert(m).values(
                id=suggestion_id, entity_type="variant", a=a, b=b, reason=NUMBERING_CONFLICT
            )
        )
        return suggestion_id

    def mark_removed(self, variant_id: str, at: datetime) -> None:
        v = catalog.unit_variant
        self._c.execute(
            sa.update(v)
            .where(v.c.id == variant_id, v.c.removed_at.is_(None))
            .values(removed_at=at, preferred=False)  # a removed Variant is no longer exported
        )

    def set_preferred(self, unit_id: str, variant_id: str | None) -> None:
        v = catalog.unit_variant
        self._c.execute(sa.update(v).where(v.c.unit_id == unit_id).values(preferred=False))
        if variant_id is not None:
            self._c.execute(sa.update(v).where(v.c.id == variant_id).values(preferred=True))

    def units_in_order(self, edition_id: str) -> list[UnitView]:
        u = catalog.unit.c
        rows = self._c.execute(
            sa.select(
                u.id,
                u.number_raw,
                u.number_norm,
                u.track,
                u.number_segment,
                u.part,
                u.source_order,
                u.number_segment_ordinal,
            ).where(u.edition_id == edition_id)
        ).all()
        ordered = sorted(
            rows,
            key=lambda r: (
                _TRACK_RANK[r.track],
                r.number_segment_ordinal,
                Decimal(r.number_norm or "0"),
                int(r.part or 1),
                r.source_order,
                r.id,
            ),
        )
        return [
            UnitView(
                r.id, r.number_raw, r.number_norm, r.track, r.number_segment, r.part, r.source_order
            )
            for r in ordered
        ]

    def numbers(self, edition_id: str) -> list[tuple[str, str, int, Decimal]]:
        """Numbered Units as `(id, segment, segment ordinal, number)`, from the stored keys."""
        u = catalog.unit.c
        rows = self._c.execute(
            sa.select(u.id, u.number_segment, u.number_segment_ordinal, u.number_norm).where(
                u.edition_id == edition_id, u.track != "unnumbered"
            )
        )
        return [
            (r.id, r.number_segment, r.number_segment_ordinal, Decimal(r.number_norm)) for r in rows
        ]

    def irreplaceable(self, edition_id: str) -> list[str]:
        """Units whose every Variant is removed: the candidates EF-BIB-042 must not invent."""
        u, v = catalog.unit.c, catalog.unit_variant.c
        live = sa.exists().where(v.unit_id == u.id, v.removed_at.is_(None))
        any_variant = sa.exists().where(v.unit_id == u.id)
        return list(
            self._c.execute(
                sa.select(u.id).where(u.edition_id == edition_id, any_variant, ~live).order_by(u.id)
            ).scalars()
        )
