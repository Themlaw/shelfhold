"""The key of the apply job carries the confirmation of a rename plan (M3 L29, EF-EXP-037)."""

import pytest

from shelfhold.export.application.rename_plan import RenameRefusedError, validate_rel_path
from shelfhold.runtime_jobs import _plan_of, apply_key


@pytest.mark.req("EF-EXP-037")
def test_the_confirmation_travels_in_the_dedupe_key_and_is_part_of_its_identity() -> None:
    plain, confirmed = apply_key("P1", "abc"), apply_key("P1", "abc", confirmed=True)
    assert plain != confirmed  # a request without the confirmation never shares the job
    assert _plan_of(plain) == ("P1", "abc", False)
    assert _plan_of(confirmed) == ("P1", "abc", True)


@pytest.mark.req("EF-EXP-030")
@pytest.mark.parametrize("path", ["A/b.cbz", "Série é/x y.cbz"])
def test_a_plain_relative_path_is_accepted(path: str) -> None:
    assert validate_rel_path(path) == path


@pytest.mark.req("EF-EXP-030")
@pytest.mark.parametrize("path", ["a/" * 9 + "x.cbz", "A/" + "é" * 200 + ".cbz"])
def test_too_deep_or_too_long_is_refused(path: str) -> None:
    with pytest.raises(RenameRefusedError):
        validate_rel_path(path)
