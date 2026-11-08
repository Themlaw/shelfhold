"""The pure rules of L26: instance fingerprint, field ownership, tag-derived age, arrival."""

from datetime import timedelta
from typing import Any

import pytest

from shelfhold.export.domain.field_owner import (
    FieldAction,
    OwnedField,
    decide,
    effective_age,
    is_drift,
    should_push,
)
from shelfhold.export.domain.kavita_access import (
    Account,
    check_link,
    exposed_accounts,
    rating_for_age,
    restricted_accounts,
)
from shelfhold.export.domain.kavita_arrival import Step, next_step, probable_causes
from shelfhold.export.domain.kavita_instance import (
    instance_changed,
    instance_fingerprint,
    match_library,
)
from shelfhold.sdk.age import AgeRating
from tests.kavita_env import NOW

X18, TEEN, EVERYONE, UNKNOWN, NOT_APPLICABLE = 14, 8, 3, 0, -1


@pytest.mark.req("EF-EXP-039")
def test_fingerprint_changes_with_the_install_not_with_the_spelling_of_the_url() -> None:
    a = instance_fingerprint("http://Kavita.lan:5000/", "install-1")
    assert a == instance_fingerprint("http://kavita.lan:5000", "install-1")
    assert a != instance_fingerprint("http://kavita.lan:5000", "install-2")
    assert a != instance_fingerprint("http://kavita.lan:5001", "install-1")
    assert instance_fingerprint("http://kavita.lan:5000", None) is None
    assert not instance_changed(None, a) and not instance_changed(None, None)  # first sight
    assert instance_changed(a, None)  # an id that cannot be read any more, once one was known
    assert a != instance_fingerprint("http://kavita.lan:5000", "install-1", "2026-02-01")
    assert instance_changed(a, instance_fingerprint("http://kavita.lan:5000", "install-2"))


@pytest.mark.req("EF-EXP-039")
def test_library_is_found_by_its_folder_exact_first_then_the_deepest_ancestor() -> None:
    libraries: list[dict[str, Any]] = [
        {"id": 1, "folders": ["/manga"]},
        {"id": 2, "folders": ["/manga/Kavita"]},
        {"id": 3, "folders": ["/comics"]},
    ]
    assert (match_library(libraries, "/manga/Kavita") or {})["id"] == 2
    assert (match_library(libraries, "/manga/Kavita2") or {})["id"] == 1
    assert (match_library(libraries, "/mangas") or {}).get("id") is None
    assert match_library([], "/manga") is None


@pytest.mark.req("EF-EXP-057")
def test_field_is_rewritten_only_if_the_registry_owns_it() -> None:
    mine = OwnedField("Dr Stone", pushed=True)
    assert (
        decide(field="title", locked_in_kavita=True, kavita_value="Titre Kavita+", owned=None)
        is FieldAction.LEAVE
    )
    assert (
        decide(field="title", locked_in_kavita=False, kavita_value="x", owned=None)
        is FieldAction.WRITE
    )
    assert (
        decide(field="title", locked_in_kavita=True, kavita_value="Dr Stone", owned=mine)
        is FieldAction.NOTHING
    )
    drift = decide(field="title", locked_in_kavita=True, kavita_value="Other", owned=mine)
    assert drift is FieldAction.REPUSH and is_drift(drift)
    invalidated = OwnedField("Dr Stone", pushed=False)  # after a reinstall
    again = decide(field="age_rating", locked_in_kavita=False, kavita_value=None, owned=invalidated)
    assert again is FieldAction.PUSH_AGAIN and not is_drift(again)
    # a lock found on the new instance is a third party's, whatever the registry remembers
    taken = decide(field="age_rating", locked_in_kavita=True, kavita_value=9, owned=invalidated)
    assert taken is FieldAction.LEAVE
    other = decide(field="title", locked_in_kavita=True, kavita_value="x", owned=invalidated)
    assert other is FieldAction.LEAVE  # PUSH_AGAIN is for the age only


@pytest.mark.req("EF-EXP-057")
@pytest.mark.parametrize(
    ("shelfhold", "tags", "kept"),
    [
        (AgeRating(18), EVERYONE, AgeRating(18)),  # a tag never lowers the class
        (AgeRating(18), UNKNOWN, AgeRating(18)),
        (AgeRating(18), NOT_APPLICABLE, AgeRating(18)),
        (AgeRating(16), X18, AgeRating(18, x=True)),  # but it can raise it
        (None, TEEN, AgeRating(13)),  # without a class of its own, it is all there is
        (None, UNKNOWN, None),
    ],
)
def test_effective_age_tags_complement_never_lower(
    shelfhold: AgeRating | None, tags: int, kept: AgeRating | None
) -> None:
    assert effective_age(shelfhold, tags) == kept


@pytest.mark.req("EF-EXP-040")
@pytest.mark.parametrize(
    ("age", "x", "rating"),
    [(0, False, 3), (10, False, 5), (12, False, 8), (13, False, 8), (16, False, 10),
     (18, False, 13), (18, True, 14)],
)  # fmt: skip
def test_rating_for_age_is_the_nearest_kavita_rating_at_or_above(
    age: int, x: bool, rating: int
) -> None:
    assert rating_for_age(age, x=x) == rating


@pytest.mark.req("EF-EXP-057")
def test_should_push_never_rewrites_a_lock_of_a_third_party() -> None:
    mine = OwnedField(5, True)
    assert not should_push(FieldAction.LEAVE, wanted=8, owned=None)
    assert not should_push(FieldAction.NOTHING, wanted=5, owned=mine)  # nothing moved
    assert should_push(FieldAction.NOTHING, wanted=8, owned=mine)  # reclassified
    assert should_push(FieldAction.REPUSH, wanted=5, owned=mine)  # drift
    assert should_push(FieldAction.WRITE, wanted=5, owned=None)
    assert should_push(FieldAction.PUSH_AGAIN, wanted=5, owned=OwnedField(5, False))


def _account(name: str, **kw: Any) -> Account:
    base: dict[str, Any] = {
        "is_admin": False, "age_rating": NOT_APPLICABLE, "include_unknowns": True,
        "library_ids": frozenset({7}),
    }  # fmt: skip
    return Account(name, **{**base, **kw})


@pytest.mark.req("EF-EXP-055")
def test_exposure_rule_and_link_refusal_name_the_accounts() -> None:
    accounts = [
        _account("boss", is_admin=True, library_ids=frozenset()),
        _account("reader"),  # no restriction, unknowns included: not a child account
        _account("kid", age_rating=TEEN),
        _account("blind", include_unknowns=False),
        _account("elsewhere", age_rating=TEEN, library_ids=frozenset({8})),
    ]
    exposed = exposed_accounts(accounts, 7)
    assert [a.username for a in exposed] == ["blind", "kid"]
    assert check_link(18, exposed) == ("blind", "kid")
    assert check_link(None, exposed) == ("blind", "kid")  # an X shelf too
    # in M1a any restricted account refuses an adult shelf, tier 18 or 99 included
    assert check_link(18, exposed_accounts([_account("teen", age_rating=X18)], 7)) == ("teen",)
    assert check_link(13, exposed_accounts([_account("teen", age_rating=X18)], 7)) == ()
    assert restricted_accounts(accounts, frozenset({7, 8})) == (
        "blind",
        "elsewhere",
        "kid",
        "reader",
    )


@pytest.mark.req("EF-EXP-034")
def test_arrival_steps_wait_rescan_then_declare() -> None:
    def step(
        minutes: int, *, indexed: bool = False, rescanned: bool = False, scans: int = 2
    ) -> Step:
        return next_step(
            written_at=NOW, now=NOW + timedelta(minutes=minutes), indexed=indexed,
            rescanned=rescanned, max_scans=scans,
        )  # fmt: skip

    assert step(5) is Step.WAIT
    assert step(14, indexed=True) is Step.INDEXED
    assert step(15) is Step.RESCAN
    assert step(16, rescanned=True) is Step.WAIT  # a second scan is sent once
    assert step(30, rescanned=True) is Step.NOT_INDEXED
    assert step(20, scans=1) is Step.WAIT  # the cut version: one scan only
    assert step(30, indexed=True) is Step.INDEXED  # arriving at the last minute counts


@pytest.mark.req("EF-EXP-034")
def test_probable_causes_are_the_three_the_admin_can_fix() -> None:
    library = {"type": 1, "folderWatching": False, "excludePatterns": ["**/.shelfhold/**", "*.tmp"]}
    causes = probable_causes(library, "/manga/Kavita/.shelfhold/x.cbz", expected_type=0)
    assert causes == ("excluded_by_pattern", "wrong_library_type", "folder_watching_off")
    assert probable_causes({**library, "type": 0, "folderWatching": True}, "/m/a.cbz", 0) == ()
    assert probable_causes(None, "/m/a.cbz", 0) == ("library_missing",)
