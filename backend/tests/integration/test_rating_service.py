"""L08 (EF-BIB-036, EF-BIB-037, EF-BIB-055): the `rate` service over a real database."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    Admission,
    RatingRefusedError,
    RatingService,
)
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity
from shelfhold.sdk.age import AgeRating
from tests.catalog_env import Harness, harness

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
ALL, TEEN, ADULT, X = AgeRating(0), AgeRating(13), AgeRating(18), AgeRating(18, x=True)


def admin(**changes: object) -> Principal:
    base = {
        "user_id": new_ulid(), "username": "admin", "role": "admin",
        "credential": Credential.SESSION, "session_id": new_ulid(), "auth_time": NOW,
        "can_rate": True,
    }  # fmt: skip
    return Principal(**{**base, **changes})  # type: ignore[arg-type]


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


def shelf(h: Harness, ceiling: int | None) -> str:
    shelf_id = new_ulid()
    h.connection.execute(
        sa.insert(export.shelf).values(
            id=shelf_id, name="Family", phrase="family", max_age_rating=ceiling
        )
    )
    return shelf_id


def file_on(h: Harness, edition_id: str, shelf_id: str) -> None:
    h.connection.execute(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == edition_id)
        .values(shelf_id=shelf_id)
    )


SERVICE = RatingService()


@pytest.mark.req("EF-BIB-036")
def test_rate_goes_through_the_reserved_action_and_is_audited(h: Harness) -> None:
    edition = h.edition()
    result = SERVICE.rate(h.connection, edition.work_id, TEEN, principal=admin(), now=NOW)
    row = h.connection.execute(
        sa.select(catalog.work).where(catalog.work.c.id == edition.work_id)
    ).one()
    assert (row.age_rating, row.age_x, row.rated_at) == (13, False, NOW)
    assert result.label == "age.min.13"
    entry = h.connection.execute(
        sa.select(identity.audit).where(identity.audit.c.action == "rate")
    ).one()
    assert entry.target == f"work:{edition.work_id}"


@pytest.mark.req("EF-BIB-036")
@pytest.mark.parametrize(
    "who",
    [
        {"credential": Credential.API_KEY},
        {"credential": Credential.DEVICE_TOKEN},
        {"role": "member"},
        {"can_rate": False},
    ],
)
def test_only_an_admin_with_the_right_rates(h: Harness, who: dict[str, object]) -> None:
    edition = h.edition()
    with pytest.raises(RatingRefusedError):
        SERVICE.rate(h.connection, edition.work_id, ALL, principal=admin(**who), now=NOW)
    assert h.scalar("SELECT age_rating FROM work WHERE id = :i", i=edition.work_id) is None
    assert h.scalar("SELECT count(*) FROM audit") == 0


@pytest.mark.req("EF-BIB-036")
def test_rate_refuses_an_age_outside_the_tiers(h: Harness) -> None:
    edition = h.edition()
    with pytest.raises(ValueError, match="tier"):
        SERVICE.rate(h.connection, edition.work_id, AgeRating(7), principal=admin(), now=NOW)


@pytest.mark.req("EF-BIB-055")
def test_new_edition_after_rating_is_unrated_until_admin_rates(h: Harness) -> None:
    first = h.edition(lang="en")
    SERVICE.rate(h.connection, first.work_id, ALL, principal=admin(), now=NOW)
    family = shelf(h, 10)
    assert SERVICE.admission_of(h.connection, first.id, shelf_id=family) is Admission.ALLOWED
    later = h.edition(first.work_id, lang="fr")  # arrives after the classing
    assert h.scalar("SELECT rated_at FROM edition WHERE id = :i", i=later.id) is None
    assert (
        SERVICE.admission_of(h.connection, later.id, shelf_id=family) is Admission.UNRATED_EDITION
    )
    assert SERVICE.admission_of(h.connection, first.id, shelf_id=family) is Admission.ALLOWED
    SERVICE.rate(h.connection, first.work_id, ALL, principal=admin(), now=NOW)  # the admin rates
    assert SERVICE.admission_of(h.connection, later.id, shelf_id=family) is Admission.ALLOWED


@pytest.mark.req("EF-BIB-036", "EF-BIB-055")
def test_suggested_age_above_rating_blocks_and_never_rates(h: Harness) -> None:
    edition = h.edition()
    SERVICE.rate(h.connection, edition.work_id, ALL, principal=admin(), now=NOW)
    family = shelf(h, 10)
    h.connection.execute(
        sa.insert(catalog.age_suggestion).values(
            id=new_ulid(), entity_type="edition", entity_id=edition.id, value=18, x=True,
            provenance="sub_source", seen_at=NOW,
        )
    )  # fmt: skip
    assert (
        SERVICE.admission_of(h.connection, edition.id, shelf_id=family)
        is Admission.SUGGESTION_ABOVE
    )
    assert h.scalar("SELECT age_rating FROM work WHERE id = :i", i=edition.work_id) == 0


@pytest.mark.req("EF-BIB-037")
def test_explicit_edition_age_and_effective_age_of_the_work(h: Harness) -> None:
    uncensored = h.edition(lang="en")
    kids = h.edition(uncensored.work_id, lang="fr")
    SERVICE.rate(h.connection, uncensored.work_id, ADULT, principal=admin(), now=NOW)
    SERVICE.rate_edition(h.connection, kids.id, ALL, principal=admin(), now=NOW)
    assert SERVICE.effective_age_of(h.connection, uncensored.work_id) == ADULT
    family = shelf(h, 10)
    assert SERVICE.admission_of(h.connection, kids.id, shelf_id=family) is Admission.ALLOWED
    assert (
        SERVICE.admission_of(h.connection, uncensored.id, shelf_id=family)
        is Admission.ABOVE_CEILING
    )


@pytest.mark.req("EF-BIB-037")
def test_raising_the_age_plans_the_withdrawal_from_incompatible_shelves(h: Harness) -> None:
    edition = h.edition()
    SERVICE.rate(h.connection, edition.work_id, ALL, principal=admin(), now=NOW)
    family = shelf(h, 10)
    file_on(h, edition.id, family)
    result = SERVICE.rate(h.connection, edition.work_id, ADULT, principal=admin(), now=NOW)
    assert [(w.edition_id, w.shelf_id, w.reason) for w in result.withdrawals] == [
        (edition.id, family, Admission.ABOVE_CEILING)
    ]
    # a plan, not an action: the Edition is still filed until the admin applies it
    assert h.scalar("SELECT shelf_id FROM edition WHERE id = :i", i=edition.id) == family


def suggest(h: Harness, edition_id: str, age: AgeRating, provenance: str) -> None:
    h.connection.execute(
        sa.insert(catalog.age_suggestion).values(
            id=new_ulid(), entity_type="edition", entity_id=edition_id, value=age.min_age,
            x=age.x, provenance=provenance, seen_at=NOW,
        )
    )  # fmt: skip


def states(h: Harness) -> list[str]:
    return [r[0] for r in h.connection.execute(
        sa.select(catalog.age_suggestion.c.state).order_by(catalog.age_suggestion.c.provenance)
    )]  # fmt: skip


@pytest.mark.req("EF-BIB-055")
def test_rating_a_work_does_not_settle_a_higher_edition_suggestion(h: Harness) -> None:
    """Fail closed: the admin rating the Work did not see the Edition's X suggestion (it is not
    in the list she rates from): it stays open and keeps the Edition out of the shelf."""
    edition = h.edition()
    family = shelf(h, 10)
    suggest(h, edition.id, X, "sub_source")
    result = SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    assert (
        SERVICE.admission_of(h.connection, edition.id, shelf_id=family)
        is Admission.SUGGESTION_ABOVE
    )
    assert states(h) == ["open"]
    assert [(s.edition_id, s.age) for s in result.open_suggestions] == [(edition.id, X)]
    assert [w.reason for w in result.withdrawals] == []  # nothing is filed there yet
    # the way out is an explicit rating of the Edition, which rules on its own suggestions
    SERVICE.rate_edition(h.connection, edition.id, AgeRating(10), principal=admin(), now=NOW)
    assert SERVICE.admission_of(h.connection, edition.id, shelf_id=family) is Admission.ALLOWED
    assert states(h) == ["rejected"]
    settle = h.connection.execute(
        sa.select(identity.audit.c.target).where(identity.audit.c.action == "age_suggestion.settle")
    ).scalar_one()
    assert settle.endswith("accepted:0;rejected:1")


@pytest.mark.req("EF-BIB-055")
def test_rating_a_work_settles_no_edition_suggestion(h: Harness) -> None:
    edition = h.edition()
    suggest(h, edition.id, AgeRating(10), "comicinfo")
    SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    assert states(h) == ["open"]  # not above: it blocks nothing, and only rate_edition rules


@pytest.mark.req("EF-BIB-055")
def test_a_higher_edition_suggestion_is_listed_to_rate_with_its_edition(h: Harness) -> None:
    from shelfhold.export.application.to_rate import to_rate

    edition = h.edition()
    shelf(h, 10)
    SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    assert to_rate(h.connection) == []
    suggest(h, edition.id, X, "sub_source")  # arrives after the rating (or was left open by it)
    listed = to_rate(h.connection)
    assert [i.work_id for i in listed] == [edition.work_id]
    assert [(s.edition_id, s.age, s.provenance) for s in listed[0].suggestions] == [
        (edition.id, X, "sub_source")
    ]


@pytest.mark.req("EF-BIB-055")
def test_suggestion_after_the_rating_blocks_again_until_rated_again(h: Harness) -> None:
    edition = h.edition()
    family = shelf(h, 10)
    SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    suggest(h, edition.id, AgeRating(10), "comicinfo")  # not above: accepted with the next rating
    suggest(h, edition.id, X, "sub_source")
    assert (
        SERVICE.admission_of(h.connection, edition.id, shelf_id=family)
        is Admission.SUGGESTION_ABOVE
    )
    SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    assert (
        SERVICE.admission_of(h.connection, edition.id, shelf_id=family)
        is Admission.SUGGESTION_ABOVE
    )  # rating the Work does not rule on a higher suggestion of an Edition
    assert states(h) == ["open", "open"]
    SERVICE.rate_edition(h.connection, edition.id, AgeRating(10), principal=admin(), now=NOW)
    assert SERVICE.admission_of(h.connection, edition.id, shelf_id=family) is Admission.ALLOWED
    assert states(h) == ["accepted", "rejected"]


@pytest.mark.req("EF-BIB-055")
def test_rating_an_edition_settles_only_its_own_suggestions(h: Harness) -> None:
    mine = h.edition(lang="en")
    other = h.edition(mine.work_id, lang="fr")
    suggest(h, mine.id, X, "sub_source")
    suggest(h, other.id, X, "comicinfo")
    SERVICE.rate_edition(h.connection, mine.id, AgeRating(10), principal=admin(), now=NOW)
    assert states(h) == ["open", "rejected"]  # comicinfo (the other Edition) stays open


@pytest.mark.req("EF-BIB-055")
def test_sibling_rating_and_rerating_keep_the_block(h: Harness) -> None:
    from shelfhold.export.application.to_rate import SUGGESTION_ABOVE, rateable, to_rate

    e1 = h.edition()
    e2 = h.edition(e1.work_id, lang="fr")
    family = shelf(h, 10)
    suggest(h, e1.id, X, "sub_source")
    SERVICE.rate(h.connection, e1.work_id, AgeRating(10), principal=admin(), now=NOW)
    SERVICE.rate_edition(h.connection, e2.id, AgeRating(10), principal=admin(), now=NOW)
    SERVICE.rate(h.connection, e1.work_id, AgeRating(10), principal=admin(), now=NOW)
    assert SERVICE.admission_of(h.connection, e1.id, shelf_id=family) is Admission.SUGGESTION_ABOVE
    listed = to_rate(h.connection)
    assert [i.state for i in listed] == [SUGGESTION_ABOVE] and rateable(listed) == []


@pytest.mark.req("EF-BIB-055")
def test_x_work_rating_then_lowered_does_not_let_the_x_edition_in(h: Harness) -> None:
    """Rated X first (the suggestion is not above), lowered to 10 later: no decision about the
    Edition was ever taken, so it must not enter the child shelf."""
    edition = h.edition()
    family = shelf(h, 10)
    suggest(h, edition.id, X, "sub_source")
    SERVICE.rate(h.connection, edition.work_id, X, principal=admin(), now=NOW)
    SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    assert (
        SERVICE.admission_of(h.connection, edition.id, shelf_id=family)
        is Admission.SUGGESTION_ABOVE
    )
    assert states(h) == ["open"]
