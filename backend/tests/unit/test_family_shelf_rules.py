"""L10 (EF-EXP-002, EF-EXP-047, EF-USR-015, ENF-COMP-001): the rules of a family shelf as pure
functions and through the one planner. No disk, no database."""

import pytest

from shelfhold.catalog.public import Admission, EditionAge, admission, ceiling_of
from shelfhold.export.domain.bindings import (
    BindingFacts,
    LibraryNotDedicatedError,
    RemoteFolderSharedError,
    ShelfCeilingError,
    UnpartitionedDestinationError,
    check_binding,
    check_ceiling,
    check_partitioning,
    effective_partitioning,
)
from shelfhold.export.domain.kavita_compat import blocks_family_shelf
from shelfhold.export.domain.kavita_instance import library_is_dedicated
from shelfhold.export.domain.planner import (
    DestinationFacts,
    EditionFacts,
    PlanRequest,
    SeriesFacts,
    UnitFacts,
    plan_destination,
)
from shelfhold.sdk.age import AgeRating
from tests.fakes.kavita.plugin_parse import simulate_parse

LIBRARY = "01JLIBRARY00000000000000A1"
RATED = Admission.ALLOWED.value


def destination(**extra: object) -> DestinationFacts:
    values: dict[str, object] = {
        "shelf_destination_id": "dest-1", "shelf_id": "shelf-1", "remote_library_id": LIBRARY,
        "max_age_rating": 13,
    }  # fmt: skip
    return DestinationFacts(**{**values, **extra})  # type: ignore[arg-type]


def edition(
    edition_id: str = "ed-1", work_id: str = "w-1", title: str = "Dr Stone"
) -> EditionFacts:
    return EditionFacts(edition_id=edition_id, work_id=work_id, work_title=title, lang="fr")


def unit(unit_id: str = "u-1", edition_id: str = "ed-1", number: str = "1") -> UnitFacts:
    return UnitFacts(
        unit_id=unit_id,
        edition_id=edition_id,
        number_norm=number,
        content_hash="sha256:" + "a" * 64,
    )


def request(**extra: object) -> PlanRequest:
    values: dict[str, object] = {
        "destination": destination(), "editions": (edition(),), "units": (unit(),),
        "admissions": {"ed-1": RATED},
    }  # fmt: skip
    return PlanRequest(**{**values, **extra})  # type: ignore[arg-type]


def codes(plan: object) -> list[str]:
    return [b.code for b in plan.blocked]  # type: ignore[attr-defined]


@pytest.mark.req("EF-EXP-002")
def test_shelf_below_18_is_allowed_on_the_scale_of_tiers_only() -> None:
    """The M1a refusal (`test_m1a_shelf_below_18_refused`) is lifted: a ceiling is a tier."""
    for ceiling in (None, 0, 10, 12, 13, 16, 18, 99):
        check_ceiling(ceiling)
    for ceiling in (1, 3, 7, 11, 17, 100, -1):
        with pytest.raises(ShelfCeilingError) as raised:
            check_ceiling(ceiling)
        assert raised.value.code == "shelf.ceiling_invalid"


@pytest.mark.req("EF-USR-015", "EF-BIB-055")
def test_unrated_work_blocked_from_non_adult_shelf() -> None:
    unrated = request(admissions={"ed-1": Admission.UNRATED_EDITION.value})
    plan = plan_destination(unrated, simulate_parse)
    assert plan.ops == () and codes(plan) == ["shelf.rating_required"]
    fail_closed = request(admissions={})  # an Edition the loader said nothing about
    assert codes(plan_destination(fail_closed, simulate_parse)) == ["shelf.rating_required"]
    adult = request(destination=destination(max_age_rating=18), admissions={})
    assert [o.unit_id for o in plan_destination(adult, simulate_parse).ops] == ["u-1"]
    no_ceiling = request(destination=destination(max_age_rating=None), admissions={})
    assert [o.unit_id for o in plan_destination(no_ceiling, simulate_parse).ops] == ["u-1"]


@pytest.mark.req("EF-USR-015")
def test_work_above_shelf_max_blocked() -> None:
    over = request(admissions={"ed-1": Admission.ABOVE_CEILING.value})
    assert codes(plan_destination(over, simulate_parse)) == ["shelf.above_ceiling"]
    suggested = request(admissions={"ed-1": Admission.SUGGESTION_ABOVE.value})
    assert codes(plan_destination(suggested, simulate_parse)) == ["shelf.suggestion_above"]
    assert plan_destination(request(), simulate_parse).ops != ()


@pytest.mark.req("EF-BIB-037")
def test_explicit_edition_age_admits_youth_edition() -> None:
    """The adaptation for children of an 18+ Work is admitted on its own age (GAP-4-12)."""
    work_adult = AgeRating(18)
    youth = EditionAge(explicit=AgeRating(10), rated=True, work=work_adult, suggested=())
    original = EditionAge(explicit=None, rated=True, work=work_adult, suggested=())
    ceiling = ceiling_of(13)
    assert admission(youth, ceiling) is Admission.ALLOWED
    assert admission(original, ceiling) is Admission.ABOVE_CEILING


@pytest.mark.req("EF-EXP-047")
def test_family_shelf_to_unpartitioned_destination_refused() -> None:
    assert effective_partitioning("kavita", "none", attested=False) == "library"
    assert effective_partitioning("drop", "none", attested=False) == "none"
    assert effective_partitioning("drop", "user", attested=False) == "none"  # a default, not a fact
    assert effective_partitioning("drop", "user", attested=True) == "user"
    for partitioning in ("library", "user"):
        check_partitioning(13, partitioning)
    for ceiling in (18, 99, None):
        check_partitioning(ceiling, "none")  # an adult shelf may use any destination
    with pytest.raises(UnpartitionedDestinationError) as raised:
        check_partitioning(13, "none")
    assert raised.value.code == "shelf.destination_not_partitioned"
    blocked = plan_destination(
        request(destination=destination(partitioning="none")), simulate_parse
    )
    assert blocked.ops == () and codes(blocked) == ["shelf.destination_not_partitioned"]


@pytest.mark.req("EF-EXP-047", "EF-EXP-055")
def test_family_library_must_be_dedicated_and_folders_never_shared() -> None:
    mine = {"id": 1, "folders": ["/manga/Famille"]}
    assert library_is_dedicated(mine, "/manga/Famille")
    assert library_is_dedicated({"id": 1, "folders": ["/manga/Famille/Sub"]}, "/manga/Famille")
    assert not library_is_dedicated({"id": 7, "folders": ["/manga"]}, "/manga/Famille")
    assert not library_is_dedicated(
        {"id": 7, "folders": ["/manga/Famille", "/comics"]}, "/manga/Famille"
    )
    assert LibraryNotDedicatedError.code == "shelf.library_not_dedicated"
    a = BindingFacts("d1", "s1", "t", "tree", "foyer", "/a", None, remote_root="/manga/Kavita")
    b = BindingFacts("d2", "s2", "t", "tree", "foyer", "/b", None, remote_root="/manga/Kavita")
    with pytest.raises(RemoteFolderSharedError) as raised:
        check_binding(b, [a], retention_mode="keep")
    assert raised.value.code == "target.remote_folder_shared"
    nested = BindingFacts(
        "d2", "s2", "t", "tree", "foyer", "/b", None, remote_root="/manga/Kavita/Famille"
    )
    with pytest.raises(RemoteFolderSharedError):
        check_binding(nested, [a], retention_mode="keep")
    other_server = BindingFacts(
        "d2", "s2", "t2", "tree", "foyer", "/b", None, remote_root="/manga/Kavita"
    )
    check_binding(other_server, [a], retention_mode="keep")  # another server: not shared


@pytest.mark.req("ENF-COMP-001", "EF-EXP-055")
def test_kavita_known_bad_access_control_blocks_family_shelf() -> None:
    assert blocks_family_shelf("0.9.0.2")  # in the range of the advisory
    assert blocks_family_shelf("0.9.0.1")
    assert blocks_family_shelf(None)  # nobody read the version: not verifiable
    assert not blocks_family_shelf("0.9.1")
    assert not blocks_family_shelf("0.9.1.4")
    bad = request(destination=destination(access_control_known_bad=True))
    plan = plan_destination(bad, simulate_parse)
    assert plan.ops == () and codes(plan) == ["target.child_exposure"]
    adult = request(destination=destination(max_age_rating=18, access_control_known_bad=True))
    assert plan_destination(adult, simulate_parse).ops != ()  # the adult road is M1a's warning


@pytest.mark.req("EF-BIB-037", "EF-EXP-062")
def test_kavita_series_single_shelf_per_remote_library() -> None:
    """A series of the library made by another shelf is never written into by this one."""
    other = SeriesFacts(
        "series-1",
        LIBRARY,
        "Dr Stone",
        "dr stone",
        frozenset({"w-1"}),
        frozenset({"ed-1"}),
        shelf_destination_id="dest-other",
    )
    taken = request(series=(other,), edition_series={"ed-1": "series-1"})
    plan = plan_destination(taken, simulate_parse)
    assert plan.ops == () and codes(plan) == ["export.series_other_shelf"]
    mine = SeriesFacts(
        "series-1",
        LIBRARY,
        "Dr Stone",
        "dr stone",
        frozenset({"w-1"}),
        frozenset({"ed-1"}),
        shelf_destination_id="dest-1",
    )
    own = request(series=(mine,), edition_series={"ed-1": "series-1"})
    assert [o.unit_id for o in plan_destination(own, simulate_parse).ops] == ["u-1"]


@pytest.mark.req("EF-EXP-062", "EF-EXP-055")
def test_family_shelf_never_shares_a_remote_library_with_another_tier() -> None:
    mixed = request(library_ceilings=frozenset({13, 99}))
    plan = plan_destination(mixed, simulate_parse)
    assert plan.ops == () and codes(plan) == ["export.age_tier_mix"]
    adults = request(
        destination=destination(max_age_rating=18), library_ceilings=frozenset({18, 99})
    )
    plan = plan_destination(adults, simulate_parse)
    assert plan.ops != () and [w.code for w in plan.warnings] == ["age_tier_mix"]


# -- ENF-QUAL-006: the barrier as a property of the planner ----------------------------------

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

AGES = st.one_of(
    st.none(), st.builds(AgeRating, st.sampled_from([0, 10, 12, 13, 16, 18]), st.booleans())
)


@pytest.mark.req("ENF-QUAL-006", "EF-BIB-055")
@settings(max_examples=200, deadline=None)
@given(
    ceiling=st.one_of(st.none(), st.sampled_from([0, 10, 12, 13, 16, 18, 99])),
    explicit=AGES,
    work=AGES,
    rated=st.booleans(),
    suggested=st.lists(
        st.builds(AgeRating, st.sampled_from([0, 10, 13, 16, 18]), st.booleans()), max_size=2
    ),
)
def test_no_op_for_an_edition_the_admission_refuses(
    ceiling: int | None,
    explicit: AgeRating | None,
    work: AgeRating | None,
    rated: bool,
    suggested: list[AgeRating],
) -> None:
    """Whatever the ages, an operation exists for a family shelf only when `admission` allows."""
    verdict = admission(EditionAge(explicit, rated, work, tuple(suggested)), ceiling_of(ceiling))
    plan = plan_destination(
        request(
            destination=destination(max_age_rating=ceiling), admissions={"ed-1": verdict.value}
        ),
        simulate_parse,
    )
    family = ceiling is not None and ceiling < 18
    if family and verdict is not Admission.ALLOWED:
        assert plan.ops == () and plan.blocked
    else:
        assert plan.ops
