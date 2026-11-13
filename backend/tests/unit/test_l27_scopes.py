"""M3 L27 (EF-BIB-054 (4)): how a reference is folded before it is compared with a tombstone.

A tombstone keeps the reference as it was received; an alias, a migration or a re-typed URL may
spell the same series differently. The fold is the one place that says which spellings are one."""

from __future__ import annotations

import pytest

from shelfhold.library.tombstones.scopes import EmptyRefError, alias_family, fold_ref, safe_fold

pytestmark = pytest.mark.req("EF-SRC-022", "EF-BIB-054")


@pytest.mark.parametrize(
    "spelling",
    [
        "gone-1",
        "GONE-1",
        "Gone-1",
        " gone-1 ",
        "gone-1/",
        "/gone-1",
        "//gone-1//",
        "gone%2D1",
        "\uff47one-1",
        "gone%252D1",
        "gone\u00ad-1",
        "gone\u200b-1",
        "gone-1?",
        "gone-1#",
        "gone-1?x=1#top",
        "gone-1/#",
    ],
)
def test_spellings_of_one_reference_fold_together(spelling: str) -> None:
    assert fold_ref(spelling) == fold_ref("gone-1")


@pytest.mark.parametrize("other", ["gone-2", "gone", "gone-1/x", "one-1"])
def test_other_references_do_not_fold_together(other: str) -> None:
    assert fold_ref(other) != fold_ref("gone-1")


@pytest.mark.parametrize("empty", ["", "/", "///", "?", "#", " \u200b ", "?x=1", "\u00ad"])
def test_a_reference_with_nothing_left_is_refused(empty: str) -> None:
    with pytest.raises(EmptyRefError):
        fold_ref(empty)
    assert safe_fold(empty) == empty.casefold()  # a graph node still exists for it


def test_fold_is_idempotent() -> None:
    for raw in ("A/B/", "x%252Fy", "ǅ", "Ab?c#d"):
        assert fold_ref(fold_ref(raw)) == fold_ref(raw)


def test_alias_family_is_the_component_every_scheme_both_directions() -> None:
    edges = [("k", "a", "k", "b"), ("k", "b", "k", "c"), ("k", "x", "k", "c"), ("k", "z", "k", "w")]
    assert alias_family(edges, ("k", "A")) == (("k", "a"), ("k", "b"), ("k", "c"), ("k", "x"))
    assert alias_family(edges, ("k", "x")) == alias_family(edges, ("k", "a"))  # from any member
    assert alias_family(edges, ("k", "alone")) == (("k", "alone"),)
    loop = [("k", "a", "k", "b"), ("k", "b", "k", "a")]  # a cycle ends
    assert alias_family(loop, ("k", "a")) == (("k", "a"), ("k", "b"))
    wide = [("k", f"n{i}", "k", f"n{i + 1}") for i in range(10)]
    assert alias_family(wide, ("k", "n0"), limit=5) is None  # too large to follow: blocked
