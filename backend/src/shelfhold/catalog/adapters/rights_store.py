"""SQL side of the rights review: credits, death years, the instance country, the cache columns."""

import sqlalchemy as sa

from shelfhold.catalog.application.rights import EditionFacts, year_of
from shelfhold.catalog.domain.public_domain import Holder
from shelfhold.infrastructure.schema import catalog, platform

_NO_WARNING_BASES = ("licence", "user_supplied", "worldwide_public_domain")
_FALLBACK_COUNTRY = "FR"  # no instance row yet: the project's default country


class SqlRightsStore:
    def __init__(self, connection: sa.Connection) -> None:
        self._c = connection

    def facts(self, edition_id: str) -> EditionFacts | None:
        edition, work = catalog.edition.c, catalog.work.c
        row = self._c.execute(
            sa.select(edition.work_id, edition.rights_basis, work.first_published)
            .select_from(catalog.edition.join(catalog.work, work.id == edition.work_id))
            .where(edition.id == edition_id)
        ).first()
        if row is None:
            return None
        credit, person = catalog.contribution.c, catalog.contributor.c
        credited = self._c.execute(
            sa.select(
                person.id,
                person.primary_name,
                person.death_date,
                person.died_for_france,
                credit.role,
            )
            .select_from(
                catalog.contribution.join(catalog.contributor, person.id == credit.contributor_id)
            )
            .where(
                sa.or_(
                    sa.and_(credit.entity_type == "work", credit.entity_id == row.work_id),
                    sa.and_(credit.entity_type == "edition", credit.entity_id == edition_id),
                )
            )
            .order_by(credit.position, person.id, credit.role)
        ).all()
        seen: set[tuple[str, str]] = set()
        holders: list[Holder] = []
        for c in credited:
            if (c.id, c.role) in seen:
                continue
            seen.add((c.id, c.role))
            holders.append(
                Holder(c.role, year_of(c.death_date), c.died_for_france, contributor_id=c.id)
            )
        return EditionFacts(
            edition_id, row.work_id, row.rights_basis, row.first_published, tuple(holders),
            {c.id: c.primary_name for c in credited},
        )  # fmt: skip

    def jurisdiction(self) -> tuple[str, bool]:
        row = self._c.execute(
            sa.select(
                platform.instance.c.jurisdiction, platform.instance.c.jurisdiction_confirmed_at
            )
        ).first()
        if row is None:
            return _FALLBACK_COUNTRY, False
        return str(row.jurisdiction), row.jurisdiction_confirmed_at is not None

    def candidates(self) -> list[str]:
        basis = catalog.edition.c.rights_basis
        return list(
            self._c.execute(
                sa.select(catalog.edition.c.id)
                .where(sa.or_(basis.is_(None), basis.not_in(_NO_WARNING_BASES)))
                .order_by(catalog.edition.c.id)
            ).scalars()
        )

    def write_cache(
        self,
        edition_id: str,
        author_death_year: int | None,
        translator: str | None,
        translator_death_year: int | None,
    ) -> None:
        values = {
            "author_death_year": author_death_year,
            "translator": translator,
            "translator_death_year": translator_death_year,
        }
        derived = {k: v for k, v in values.items() if v is not None}
        if derived:
            self._c.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == edition_id)
                .values(**derived)
            )
