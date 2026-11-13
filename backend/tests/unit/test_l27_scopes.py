"""M3 L27 (EF-BIB-054 (4)): how a reference is folded before it is compared with a tombstone.

A tombstone keeps the reference as it was received; an alias, a migration or a re-typed URL may
spell the same series differently. The fold is the one place that says which spellings are one."""

from __future__ import annotations

import pytest
from shelfhold.library.tombstones.scopes import fold_ref

pytestmark = pytest.mark.req("EF-SRC-022", "EF-BIB-054")


@pytest.mark.parametrize(
    "spelling",
    ["gone-1", "GONE-1", "Gone-1", " gone-1 ", "gone-1/", "gone%2D1", "\uff47one-1", "gone%252D1"],
)
def test_spellings_of_one_reference_fold_together(spelling: str) -> None:
    assert fold_ref(spelling) == fold_ref("gone-1")


@pytest.mark.parametrize("other", ["gone-2", "gone", "gone-1/x", "one-1"])
def test_other_references_do_not_fold_together(other: str) -> None:
    assert fold_ref(other) != fold_ref("gone-1")


def test_fold_is_idempotent_and_never_empties_a_reference() -> None:
    for raw in ("A/B/", "x%252Fy", "ǅ", "/"):
        assert fold_ref(fold_ref(raw)) == fold_ref(raw)
    assert fold_ref("/") != ""  # a reference that is only a separator keeps something to compare
