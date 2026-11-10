"""L15: the indicative public-domain warning, by country of the instance, over every contribution.

Real migrated database, real tables: a contributor with a death date, a contribution with a MARC
role, an Edition with a `rights_basis`. The warning is an event and a line of `status`; it never
refuses anything (EF-SRC-024, ADR-0032).
"""

from datetime import UTC, date, datetime

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import RightsService
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, platform
from tests.catalog_env import Harness, harness
from tests.fakes_clock import ManualClock
from tests.headless_env import headless

TODAY = date(2026, 11, 11)
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def _instance(h: Harness, jurisdiction: str, *, confirmed: bool = True) -> None:
    h.connection.execute(
        sa.insert(platform.instance).values(
            instance_id=new_ulid(),
            jurisdiction=jurisdiction,
            jurisdiction_confirmed_at=NOW if confirmed else None,
        )
    )


def _person(h: Harness, name: str, death_year: int | None) -> str:
    contributor_id = new_ulid()
    h.connection.execute(
        sa.insert(catalog.contributor).values(
            id=contributor_id,
            kind="person",
            primary_name=name,
            death_date=None if death_year is None else str(death_year),
            death_precision=None if death_year is None else "year",
        )
    )
    return contributor_id


def _credit(h: Harness, contributor_id: str, edition_id: str, work_id: str, role: str) -> None:
    h.connection.execute(
        sa.insert(catalog.contribution).values(
            contributor_id=contributor_id, entity_type="work", entity_id=work_id, role=role
        )
    )


def _book(h: Harness, *, basis: str | None = "us_public_domain", first_published: str = "1930"):  # type: ignore[no-untyped-def]
    work_id = h.work("novel", "Un livre")
    h.connection.execute(
        sa.update(catalog.work)
        .where(catalog.work.c.id == work_id)
        .values(first_published=first_published, date_precision="year")
    )
    edition = h.edition(work_id, kind="novel", lang="fr", translation_type="official", media="text")
    h.connection.execute(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == edition.id)
        .values(rights_basis=basis, rights_jurisdiction="US")
    )
    return edition


@pytest.mark.req("EF-SRC-024")
def test_public_domain_warning_by_jurisdiction_non_blocking() -> None:
    with harness() as h:
        _instance(h, "FR")
        edition = _book(h)
        author = _person(h, "Auteur", 1944)
        _credit(h, author, edition.id, edition.work_id, "aut")
        rights = RightsService(h.connection, bus=EventBus(ManualClock(NOW)))
        report = rights.review(edition.id, today=TODAY, died_for_france=frozenset({author}))
        assert report.verdict == "may_be_protected"
        assert report.jurisdiction == "FR"
        events = h.rows("SELECT type, payload FROM event ORDER BY id")
        assert [e["type"] for e in events] == ["ops.rights_warning"]
        assert "Auteur" not in str(events)  # ids and codes only
        # non blocking: the Edition is there, a second Edition of the Work is still accepted
        again = h.edition(edition.work_id, kind="novel", lang="en", media="text")
        assert again.id != edition.id
        # the same book on a US instance: published 1930, clear (US counts from first publication)
        h.connection.execute(sa.update(platform.instance).values(jurisdiction="US"))
        assert rights.review(edition.id, today=TODAY).verdict == "no_warning"
        # a CC BY-SA licence never warns, whatever the dates
        h.connection.execute(sa.update(platform.instance).values(jurisdiction="FR"))
        h.connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition.id)
            .values(rights_basis="licence", rights="CC BY-SA 4.0")
        )
        assert rights.review(edition.id, today=TODAY).verdict == "no_warning"
        assert len(h.rows("SELECT id FROM event")) == 1  # nothing new announced


@pytest.mark.req("EF-SRC-024")
def test_translation_rights_warning() -> None:
    with harness() as h:
        _instance(h, "FR")
        edition = _book(h, first_published="1880")
        author = _person(h, "Auteur", 1885)
        translator = _person(h, "Traductrice", 1986)  # died 40 years ago
        _credit(h, author, edition.id, edition.work_id, "aut")
        _credit(h, translator, edition.id, edition.work_id, "trl")
        report = RightsService(h.connection).review(edition.id, today=TODAY)
        assert report.verdict == "may_be_protected"
        assert [w.code for w in report.warnings] == ["translation_may_be_protected"]


@pytest.mark.req("EF-BIB-046", "EF-SRC-024")
def test_public_domain_uses_all_contributions() -> None:
    with harness() as h:
        _instance(h, "FR")
        edition = _book(h, first_published="1930")
        author = _person(h, "Auteur", 1936)  # died 90 years ago
        illustrator = _person(h, "Illustrateur", 1976)  # died 50 years ago
        _credit(h, author, edition.id, edition.work_id, "aut")
        rights = RightsService(h.connection)
        assert rights.review(edition.id, today=TODAY).verdict == "no_warning"  # the author alone
        _credit(h, illustrator, edition.id, edition.work_id, "ill")
        report = rights.review(edition.id, today=TODAY)
        assert report.verdict == "may_be_protected"
        assert [w.role for w in report.warnings] == ["ill"]
        # the columns of the Edition are a cache derived from the contributions
        rights.refresh_cache(edition.id)
        row = h.rows(
            "SELECT author_death_year, translator, translator_death_year FROM edition WHERE id=:e",
            e=edition.id,
        )[0]
        assert row == {"author_death_year": 1936, "translator": None, "translator_death_year": None}


@pytest.mark.req("EF-SRC-024")
def test_jurisdiction_from_locale_unconfirmed_until_config_set(settings) -> None:  # type: ignore[no-untyped-def]
    from shelfhold.ops.status import jurisdiction_report

    with headless(settings) as env:
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR")
            )
        with env.engine.connect() as connection:
            before = jurisdiction_report(connection)
        assert before == {"jurisdiction": "FR", "confirmed": False}
        env.admin.set("instance.jurisdiction", "be")
        with env.engine.connect() as connection:
            after = jurisdiction_report(connection)
            stamp = connection.execute(
                sa.select(platform.instance.c.jurisdiction_confirmed_at)
            ).scalar_one()
        assert after == {"jurisdiction": "BE", "confirmed": True}
        assert stamp is not None
        # the (still unconfirmed) value is what the calculation uses
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(platform.instance).values(
                    jurisdiction="FR", jurisdiction_confirmed_at=None
                )
            )
        with env.engine.connect() as connection:
            assert RightsService(connection).jurisdiction() == ("FR", False)


@pytest.mark.req("EF-SRC-024")
def test_status_lists_the_editions_that_may_still_be_protected() -> None:
    from shelfhold.ops.status import rights_report

    with harness() as h:
        _instance(h, "FR", confirmed=False)
        edition = _book(h, first_published="1930")
        _credit(h, _person(h, "Auteur", 1990), edition.id, edition.work_id, "aut")
        found = rights_report(h.connection, TODAY)
        assert found == [{"edition_id": edition.id, "reason": "may_be_protected"}]
