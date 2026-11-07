"""L09 (EF-BIB-029, EF-BIB-035, EF-BIB-055): `relink` over a real database.

A new source binding is attached to an existing Work; only the numbers the Edition lacks are
asked for, the Team and its aliases follow, and an Edition that is new for a rated Work waits in
"À classer" instead of entering a shelf below 18+.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    IncomingUnit,
    ListedUnit,
    RatingService,
    RelinkRefusedError,
    RelinkRequest,
    RelinkService,
)
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity
from shelfhold.sdk.age import AgeRating
from tests.catalog_env import Harness, at, harness

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SERVICE = RelinkService()


def admin(**changes: object) -> Principal:
    base = {
        "user_id": new_ulid(), "username": "admin", "role": "admin",
        "credential": Credential.LOCAL_CLI, "session_id": None, "auth_time": None,
        "can_rate": True,
    }  # fmt: skip
    return Principal(**{**base, **changes})  # type: ignore[arg-type]


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


def listing(last: int) -> tuple[ListedUnit, ...]:
    return tuple(ListedUnit(str(n), n - 1) for n in range(1, last + 1))


def request(work_id: str, last: int = 52, **changes: object) -> RelinkRequest:
    values: dict[str, object] = {
        "work_id": work_id, "plugin_id": "suwayomi", "source_key": "src-new",
        "remote_id": new_ulid(), "listed": listing(last),
    }  # fmt: skip
    return RelinkRequest(**{**values, **changes})  # type: ignore[arg-type]


def fifty_rated(h: Harness, *, team: str | None = "Team A"):  # type: ignore[no-untyped-def]
    edition = h.edition()
    binding = h.binding(edition)
    for n in range(1, 51):
        h.catalog.ingest(
            IncomingUnit(
                binding=binding,
                remote_unit_id=f"old-{n}",
                raw_number=str(n),
                seen_at=at(n),
                source_order=n - 1,
                team_label=team,
            )
        )
    return edition, binding


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


@pytest.mark.req("EF-BIB-029")
def test_relink_fetches_only_missing_numbers(h: Harness) -> None:
    edition, old = fifty_rated(h)
    result = SERVICE.relink(
        h.connection, request(edition.work_id, replaces_binding_id=old.id), principal=admin(),
        now=NOW,
    )  # fmt: skip
    assert [m.raw_number for m in result.missing] == ["51", "52"]
    assert result.edition_id == edition.id and not result.edition_created
    active = h.rows("SELECT id FROM source_binding WHERE edition_id = :e AND active", e=edition.id)
    assert [r["id"] for r in active] == [result.binding_id]  # the old one is retired
    assert h.scalar("SELECT count(*) FROM unit WHERE edition_id = :e", e=edition.id) == 50
    again = SERVICE.relink(
        h.connection, request(edition.work_id, remote_id="other", listed=listing(50)),
        principal=admin(), now=NOW,
    )  # fmt: skip
    assert again.missing == ()  # nothing to fetch when the source lists no more than we hold


@pytest.mark.req("EF-BIB-029")
def test_relink_counts_an_unnumbered_extra_once(h: Harness) -> None:
    edition = h.edition()
    binding = h.binding(edition)
    h.catalog.ingest(
        IncomingUnit(binding=binding, remote_unit_id="x", raw_number="Extra", seen_at=at(0),
                     source_order=3)
    )  # fmt: skip
    listed = (ListedUnit("Extra", 3), ListedUnit("Extra", 4))
    result = SERVICE.relink(
        h.connection, request(edition.work_id, listed=listed), principal=admin(), now=NOW
    )
    assert [m.source_order for m in result.missing] == [4]


@pytest.mark.req("EF-BIB-055")
def test_relink_new_edition_into_rated_work_goes_to_a_classer(h: Harness) -> None:
    edition, _old = fifty_rated(h)
    family = shelf(h, 12)
    file_on(h, edition.id, family)
    RatingService().rate(h.connection, edition.work_id, AgeRating(10), principal=admin(
        credential=Credential.SESSION, session_id=new_ulid(), auth_time=NOW), now=NOW)  # fmt: skip
    result = SERVICE.relink(
        h.connection, request(edition.work_id, lang="de", listed=listing(3)), principal=admin(),
        now=NOW,
    )  # fmt: skip
    assert result.edition_created and result.edition_id != edition.id
    assert result.to_rate  # "À classer": a new Edition is unrated until an admin rates it
    row = h.rows("SELECT shelf_id, rated_at FROM edition WHERE id = :e", e=result.edition_id)[0]
    assert row["shelf_id"] is None and row["rated_at"] is None
    assert [m.raw_number for m in result.missing] == ["1", "2", "3"]  # it holds nothing yet
    kept = h.rows("SELECT shelf_id FROM edition WHERE id = :e", e=edition.id)[0]
    assert kept["shelf_id"] == family  # the rated Edition stays where it was


@pytest.mark.req("EF-BIB-055")
def test_relink_new_edition_keeps_the_shelf_when_nothing_guards_it(h: Harness) -> None:
    edition, _old = fifty_rated(h)
    adults = shelf(h, None)
    file_on(h, edition.id, adults)
    result = SERVICE.relink(
        h.connection, request(edition.work_id, lang="de"), principal=admin(), now=NOW
    )
    assert result.edition_created and not result.to_rate
    row = h.rows("SELECT shelf_id FROM edition WHERE id = :e", e=result.edition_id)[0]
    assert row["shelf_id"] == adults


@pytest.mark.req("EF-BIB-029", "EF-BIB-035")
def test_relink_carries_team_aliases(h: Harness) -> None:
    edition, old = fifty_rated(h, team="Team  A")
    teams_before = h.scalar("SELECT count(*) FROM team")
    priority = h.catalog.team_priority(edition.id)
    result = SERVICE.relink(
        h.connection, request(edition.work_id, replaces_binding_id=old.id), principal=admin(),
        now=NOW,
    )  # fmt: skip
    assert h.scalar("SELECT count(*) FROM team") == teams_before  # I27: no new Team
    moved = h.rows("SELECT label FROM team_alias WHERE source_binding_id = :b", b=result.binding_id)
    assert [r["label"] for r in moved] == ["Team  A"]
    assert h.rows("SELECT label FROM team_alias WHERE source_binding_id = :b", b=old.id)  # kept
    assert h.catalog.team_priority(edition.id) == priority
    # the same Team on a new Edition of the Work: a Team of that Edition, same label
    other = SERVICE.relink(
        h.connection, request(edition.work_id, lang="de"), principal=admin(), now=NOW
    )
    assert other.carried_teams == ()  # nothing to carry without a binding to replace


@pytest.mark.req("EF-BIB-029", "EF-BIB-035")
def test_relink_into_a_new_edition_carries_the_team_by_label(h: Harness) -> None:
    edition, old = fifty_rated(h)
    result = SERVICE.relink(
        h.connection,
        request(edition.work_id, lang="de", replaces_binding_id=old.id, listed=listing(2)),
        principal=admin(), now=NOW,
    )  # fmt: skip
    assert result.edition_created
    mine = h.rows("SELECT label FROM team WHERE edition_id = :e", e=result.edition_id)
    assert [r["label"] for r in mine] == ["team a"]
    assert result.carried_teams == ("Team A",)


@pytest.mark.req("EF-BIB-029")
def test_relink_is_audited_and_refused_to_a_key(h: Harness) -> None:
    edition, _old = fifty_rated(h)
    for who in ({"credential": Credential.API_KEY}, {"role": "member"}):
        with pytest.raises(RelinkRefusedError):
            SERVICE.relink(h.connection, request(edition.work_id), principal=admin(**who), now=NOW)
    assert h.scalar("SELECT count(*) FROM audit") == 0
    SERVICE.relink(h.connection, request(edition.work_id), principal=admin(), now=NOW)
    entry = h.connection.execute(
        sa.select(identity.audit).where(identity.audit.c.action == "relink")
    ).one()
    assert entry.target == f"work:{edition.work_id}"
    assert h.scalar("SELECT count(*) FROM source_item WHERE source_key = 'src-new'") == 1


@pytest.mark.req("EF-BIB-029")
def test_relink_refuses_an_unknown_work_or_a_binding_of_another_work(h: Harness) -> None:
    edition, _old = fifty_rated(h)
    foreign = h.binding(h.edition())
    with pytest.raises(LookupError):
        SERVICE.relink(h.connection, request("01JNOSUCHWORK000000000000A"), principal=admin(),
                       now=NOW)  # fmt: skip
    with pytest.raises(LookupError):
        SERVICE.relink(
            h.connection, request(edition.work_id, replaces_binding_id=foreign.id),
            principal=admin(), now=NOW,
        )  # fmt: skip
    assert h.scalar("SELECT count(*) FROM source_binding") == 2  # nothing half done
