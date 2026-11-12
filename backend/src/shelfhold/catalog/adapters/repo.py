"""SQLAlchemy Core repositories of the catalog (the only place that writes its tables).

All of them take an open `Connection`: the caller owns the transaction. Nothing here decides
anything; the rules are in `catalog.domain` and `catalog.application`.
"""

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

from shelfhold.catalog.application.dating import stored_date
from shelfhold.catalog.application.dto import (
    EditionRef,
    EditionSpec,
    IncomingUnit,
)
from shelfhold.catalog.application.ports import StoredVariant
from shelfhold.catalog.application.search import index_work
from shelfhold.catalog.domain.availability import Availability
from shelfhold.catalog.domain.identity import sort_title
from shelfhold.catalog.domain.normalizers import (
    REMOTE_UNIT_ID_NORMALIZER_ID,
    Normalized,
    normalize_remote_unit_id,
)
from shelfhold.catalog.domain.numbering import NUMBER_NORMALIZER_ID, UnitNumber
from shelfhold.catalog.domain.placement import NUMBERING_CONFLICT, Place, VariantFacts
from shelfhold.catalog.lang import LANG_NORMALIZER_ID, variant_lang
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, storage


class SqlWorkStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection

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
        index_work(self._c, work_id)  # EF-BIB-010: the index follows the write
        return work_id

    def series_of(self, work_id: str) -> str | None:
        return self._c.execute(
            sa.select(catalog.series_member.c.series_id).where(
                catalog.series_member.c.work_id == work_id
            )
        ).scalar()

    def insert_member(self, series_id: str, work_id: str, position: str) -> None:
        self._c.execute(
            sa.insert(catalog.series_member).values(
                series_id=series_id, work_id=work_id, position=position
            )
        )


class SqlContributorStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection

    def credited(self, work_id: str) -> bool:
        n = catalog.contribution.c
        return (
            self._c.execute(
                sa.select(sa.literal(1)).where(n.entity_type == "work", n.entity_id == work_id)
            ).first()
            is not None
        )

    def find(self, name: str, kind: str) -> str | None:
        c, n = catalog.contributor.c, catalog.contributor_name.c
        return self._c.execute(
            sa.select(c.id)
            .select_from(
                catalog.contributor.join(catalog.contributor_name, n.contributor_id == c.id)
            )
            .where(c.kind == kind, sa.func.lower(n.name) == name.casefold())
            .order_by(c.id)
            .limit(1)
        ).scalar()

    def insert(self, name: str, kind: str, family_first: bool) -> str:
        contributor_id = new_ulid()
        self._c.execute(
            sa.insert(catalog.contributor).values(id=contributor_id, kind=kind, primary_name=name)
        )
        self._c.execute(
            sa.insert(catalog.contributor_name).values(
                contributor_id=contributor_id, name=name, kind="display", lang="und",
                family_first=family_first,
            )
        )  # fmt: skip
        return contributor_id

    def credit(self, work_id: str, contributor_id: str, role: str, position: int) -> bool:
        n = catalog.contribution
        known = self._c.execute(
            sa.select(sa.literal(1)).where(
                n.c.contributor_id == contributor_id,
                n.c.entity_type == "work",
                n.c.entity_id == work_id,
                n.c.role == role,
            )
        ).first()
        if known is not None:
            return False
        self._c.execute(
            sa.insert(n).values(
                contributor_id=contributor_id, entity_type="work", entity_id=work_id,
                role=role, position=position,
            )
        )  # fmt: skip
        index_work(self._c, work_id)  # EF-BIB-010: the credited names are searchable
        return True

    def credit_edition(
        self, edition_id: str, contributor_id: str, role: str, position: int
    ) -> bool:
        n = catalog.contribution
        known = self._c.execute(
            sa.select(sa.literal(1)).where(
                n.c.contributor_id == contributor_id,
                n.c.entity_type == "edition",
                n.c.entity_id == edition_id,
                n.c.role == role,
            )
        ).first()
        if known is not None:
            return False
        self._c.execute(
            sa.insert(n).values(
                contributor_id=contributor_id, entity_type="edition", entity_id=edition_id,
                role=role, position=position,
            )
        )  # fmt: skip
        return True

    def set_death_year(self, contributor_id: str, year: int) -> bool:
        """Only a death the catalog does not know yet: what is written is never rewritten."""
        c = catalog.contributor
        done = self._c.execute(
            sa.update(c)
            .where(c.c.id == contributor_id, c.c.death_date.is_(None))
            .values(death_date=f"{year:04d}", death_precision="year")
        )
        return bool(done.rowcount)

    def next_position(self, work_id: str) -> int:
        n = catalog.contribution.c
        top = self._c.execute(
            sa.select(sa.func.max(n.position)).where(
                n.entity_type == "work", n.entity_id == work_id
            )
        ).scalar()
        return 0 if top is None else int(top) + 1


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
                lang_norm_by=LANG_NORMALIZER_ID,
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

    def has_team(self, edition_id: str, team_id: str) -> bool:
        t = catalog.team.c
        return (
            self._c.execute(
                sa.select(t.id).where(t.edition_id == edition_id, t.id == team_id)
            ).first()
            is not None
        )

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
                v.source_binding_id == binding_id,
                v.remote_unit_id == normalize_remote_unit_id(remote_unit_id),
            )
        ).first()
        return None if row is None else StoredVariant(*row)

    def last_revision(self, variant: StoredVariant) -> tuple[str | None, str | None, int | None]:
        """The latest revision seen, healthy or not: what the next item is compared with, so a
        suspect item seen again is the same revision, not a new one."""
        return self._revision_row(variant, healthy=False)

    def last_healthy_revision(
        self, variant: StoredVariant
    ) -> tuple[str | None, str | None, int | None]:
        """The latest revision the guard did not quarantine: all a new revision may inherit.
        A quarantined page count or hash never becomes anybody's baseline."""
        return self._revision_row(variant, healthy=True)

    def _revision_row(
        self, variant: StoredVariant, *, healthy: bool
    ) -> tuple[str | None, str | None, int | None]:
        r = catalog.variant_revision.c
        where = [r.variant_id == variant.id]
        if healthy:
            where.append(r.quarantined_at.is_(None))
        row = self._c.execute(
            sa.select(r.revision_hint, r.content_hash, r.page_count)
            .where(*where)
            .order_by(r.revision.desc())
            .limit(1)
        ).first()
        return (None, None, None) if row is None else (row[0], row[1], row[2])

    def latest_quarantine(self, variant: StoredVariant) -> str | None:
        """Why the latest revision is quarantined, None when it is not."""
        r = catalog.variant_revision.c
        row = self._c.execute(
            sa.select(r.quarantine_reason, r.quarantined_at)
            .where(r.variant_id == variant.id)
            .order_by(r.revision.desc())
            .limit(1)
        ).first()
        return None if row is None or row[1] is None else str(row[0] or "fewer_pages")

    def stored_pages(self, variant: StoredVariant) -> int | None:
        """The page count of the version that is really stored and served: the `content` of the
        Variant (the oldest one, the one exported), not the last revision seen: a chain of
        slightly smaller revisions never moves the baseline (EF-BIB-024). A Variant with no
        content yet falls back to its first healthy revision."""
        c = storage.content.c
        pages = self._c.execute(
            sa.select(c.page_count)
            .where(c.variant_id == variant.id, c.page_count.is_not(None))
            .order_by(c.created_at.asc(), c.id.asc())
            .limit(1)
        ).scalar()
        if pages is not None:
            return int(pages)
        r = catalog.variant_revision.c
        first = self._c.execute(
            sa.select(r.page_count)
            .where(
                r.variant_id == variant.id, r.quarantined_at.is_(None), r.page_count.is_not(None)
            )
            .order_by(r.revision.asc())
            .limit(1)
        ).scalar()
        return None if first is None else int(first)

    def revision_changed(self, variant: StoredVariant, item: IncomingUnit) -> bool:
        """Only the fields the source gave count: an absent field (`None`) is not a change, and a
        page count the source gives for the first time fills a gap, it is not a revision."""
        hint, content_hash, pages = self.last_revision(variant)
        return any(
            (
                item.revision_hint is not None and item.revision_hint != hint,
                item.content_hash is not None and item.content_hash != content_hash,
                item.page_count is not None and pages is not None and item.page_count != pages,
            )
        )

    def add_revision(
        self, variant: StoredVariant, item: IncomingUnit, *, quarantine: str | None = None
    ) -> int:
        """Append-only: the history gains a row, the Variant's identity is not touched.

        A field the source left out keeps its previous value in the new row, so the next
        comparison does not read the gap as a change. A revision the guard quarantines is marked
        in the row it is born in (set once, never lifted) and the admin is warned.
        """
        revision = variant.revision + 1
        last = self.last_healthy_revision(variant)
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
                quarantined_at=item.seen_at if quarantine else None,
                quarantine_reason=quarantine,
            )
        )
        v = catalog.unit_variant
        self._c.execute(
            sa.update(v).where(v.c.id == variant.id).values(revision=revision, revision_hint=hint)
        )
        if quarantine:
            EventBus(SystemClock()).publish(
                self._c,
                "unit.revision_suspect",
                {"unit_id": variant.unit_id, "variant_id": variant.id, "reason": quarantine},
            )
        return revision

    def queue_revision(
        self,
        variant: StoredVariant,
        revision: int,
        *,
        nature: str,
        field: str | None,
        value: str | None,
    ) -> None:
        """A revision waits in `revision_pending`: nothing is applied from here (EF-BIB-043)."""
        self._c.execute(
            sa.insert(acquisition.revision_pending)
            .values(
                id=new_ulid(), variant_id=variant.id, revision=revision, nature=nature,
                field=field, value=value,
            )
            .prefix_with("OR IGNORE")
        )  # fmt: skip

    def apply_availability(
        self, unit_id: str, variant_id: str, availability: Availability, item: IncomingUnit
    ) -> None:
        """What the source says of the Unit. `removed` upstream never deletes anything: the
        Variant is marked, the Unit says `removed` only when no other Variant still lives, and
        every stored file stays where it is (EF-BIB-009)."""
        v, u = catalog.unit_variant, catalog.unit
        if availability.value == "removed":
            self._c.execute(
                sa.update(v)
                .where(v.c.id == variant_id, v.c.removed_at.is_(None))
                .values(removed_at=item.seen_at)
            )
            others = self._c.execute(
                sa.select(sa.func.count()).where(
                    v.c.unit_id == unit_id, v.c.id != variant_id, v.c.removed_at.is_(None)
                )
            ).scalar_one()
            if others:
                return
        self._c.execute(
            sa.update(u)
            .where(u.c.id == unit_id)
            .values(
                availability=availability.value,
                availability_detail=availability.detail,
                available_until=item.available_until,
            )
        )

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
        dated = stored_date(item.published_on, item.date_precision, item.published_at)
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
                published_on=dated[0] if dated else None,
                date_precision=dated[1] if dated else None,
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
        ).all()
        m = catalog.merge_suggestion.c
        flagged = set(
            self._c.execute(
                sa.select(m.a, m.b).where(
                    m.entity_type == "variant",
                    m.reason == NUMBERING_CONFLICT,
                    m.state == "open",
                    sa.or_(m.a.in_([r.id for r in rows]), m.b.in_([r.id for r in rows])),
                )
            ).all()
        )
        age = {r.id: (r.first_seen_at, r.id) for r in rows}
        # the one that arrived in the conflict is the newer of the pair; the older keeps its place
        in_conflict = {
            max(pair, key=age.__getitem__) for pair in flagged if pair[0] in age and pair[1] in age
        }
        return [
            VariantFacts(
                r.id,
                r.source_binding_id,
                r.team_id,
                r.first_seen_at,
                r.removed_at is not None,
                r.rank,
                r.id in in_conflict,
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
                remote_unit_id=normalize_remote_unit_id(item.remote_unit_id),
                remote_unit_id_norm_by=REMOTE_UNIT_ID_NORMALIZER_ID,
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

    def titled_variant(self, edition_id: str, norm_title: str, not_unit_id: str) -> str | None:
        """The oldest Variant of another Unit of the Edition whose normalized title is this one."""
        u, v = catalog.unit.c, catalog.unit_variant.c
        rows = self._c.execute(
            sa.select(u.id, u.title).where(
                u.edition_id == edition_id, u.id != not_unit_id, u.title.is_not(None)
            )
        )
        wanted = [r.id for r in rows if sort_title(r.title) == norm_title]
        if not wanted:
            return None
        return self._c.execute(
            sa.select(v.id).where(v.unit_id.in_(wanted)).order_by(v.first_seen_at, v.id).limit(1)
        ).scalar()

    def open_suggestion(self, first: str, second: str, reason: str) -> str:
        """One open suggestion per unordered pair and reason (the table keeps `a < b`)."""
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
            sa.insert(m).values(id=suggestion_id, entity_type="variant", a=a, b=b, reason=reason)
        )
        return suggestion_id

    def set_preferred(self, unit_id: str, variant_id: str | None) -> None:
        v = catalog.unit_variant
        self._c.execute(sa.update(v).where(v.c.unit_id == unit_id).values(preferred=False))
        if variant_id is not None:
            self._c.execute(sa.update(v).where(v.c.id == variant_id).values(preferred=True))

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
