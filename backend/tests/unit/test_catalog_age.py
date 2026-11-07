"""L08 (EF-BIB-037, EF-BIB-036, EF-BIB-055): the age of a Work, of an Edition, and who is admitted."""

import pytest

from shelfhold.catalog.public import (
    Admission,
    EditionAge,
    admission,
    ceiling_of,
    content_warning_age,
    effective_age,
)
from shelfhold.sdk.age import AgeRating

ALL = AgeRating(0)
TEEN = AgeRating(13)
ADULT = AgeRating(18)
X = AgeRating(18, x=True)
FAMILY = AgeRating(10)  # a shelf ceiling


@pytest.mark.req("EF-BIB-037")
def test_effective_age_is_max_over_editions() -> None:
    assert effective_age(None, [ALL, TEEN, None]) == TEEN
    assert effective_age(ALL, [TEEN, ADULT]) == ADULT
    assert effective_age(ADULT, [ALL]) == ADULT  # the Work's own class is a floor
    assert effective_age(TEEN, [ADULT, X]) == X  # X is above every age
    assert effective_age(None, [None]) is None  # nobody rated: not an invented age


@pytest.mark.req("EF-BIB-036")
def test_default_age_from_sub_source_content_warning() -> None:
    assert content_warning_age("nsfw") == X
    assert content_warning_age("mixed") is None
    assert content_warning_age("safe") is None


@pytest.mark.req("EF-BIB-036", "EF-BIB-055")
def test_suggested_age_above_rating_blocks() -> None:
    rated = EditionAge(explicit=None, rated=True, work=ALL, suggested=())
    assert admission(rated, FAMILY) is Admission.ALLOWED
    flagged = EditionAge(explicit=None, rated=True, work=ALL, suggested=(X,))
    assert admission(flagged, FAMILY) is Admission.SUGGESTION_ABOVE
    lower = EditionAge(explicit=None, rated=True, work=TEEN, suggested=(ALL,))
    assert admission(lower, AgeRating(13)) is Admission.ALLOWED  # a lower suggestion changes nothing


@pytest.mark.req("EF-BIB-037", "EF-BIB-055")
def test_explicit_edition_age_decides_its_own_admission() -> None:
    kids = EditionAge(explicit=ALL, rated=True, work=ADULT, suggested=())
    assert admission(kids, FAMILY) is Admission.ALLOWED  # the juvenile adaptation of an 18+ Work
    other = EditionAge(explicit=None, rated=True, work=ADULT, suggested=())
    assert admission(other, FAMILY) is Admission.ABOVE_CEILING


@pytest.mark.req("EF-BIB-055")
def test_unrated_edition_is_outside_every_shelf_below_18() -> None:
    fresh = EditionAge(explicit=None, rated=False, work=ALL, suggested=())
    assert admission(fresh, FAMILY) is Admission.UNRATED_EDITION
    assert admission(fresh, ADULT) is Admission.ALLOWED  # only shelves below 18+ are guarded
    assert admission(fresh, None) is Admission.ALLOWED  # no ceiling, no guard


@pytest.mark.req("EF-BIB-037", "EF-BIB-055")
def test_unrated_work_is_never_below_18() -> None:
    nothing = EditionAge(explicit=None, rated=True, work=None, suggested=())
    for ceiling in (ALL, FAMILY, AgeRating(16)):
        assert admission(nothing, ceiling) is not Admission.ALLOWED
    assert admission(nothing, ADULT) is Admission.ALLOWED


@pytest.mark.req("EF-BIB-037")
def test_shelf_ceiling_99_is_x() -> None:
    assert ceiling_of(None) is None
    assert ceiling_of(99) == X
    assert ceiling_of(13) == TEEN
