"""One rule for "do these two folders overlap", shared by the binding check and the executor."""

import pytest

from shelfhold.export.domain.bindings import BindingFacts, TreeOverlapError, check_binding
from shelfhold.export.domain.trees import TreeFolder, check_tree_attachment


@pytest.mark.req("EF-EXP-058")
@pytest.mark.parametrize("transport", ["tree", "dropbox", "push"])
@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        ("/a/Kavita", "/a/kavita"),
        ("/a/Café", "/a/Café"),
        ("/a/Kavita", "/a/Kavita/sub"),
        ("/a/Kavita/sub", "/a/Kavita"),
    ],
)
def test_binding_and_executor_refuse_the_same_overlaps(
    transport: str, mine: str, theirs: str
) -> None:
    new = BindingFacts("d1", "s1", "t1", transport, "foyer", mine)
    other = BindingFacts("d2", "s2", "t2", transport, "foyer", theirs)
    with pytest.raises(TreeOverlapError):
        check_binding(new, [other], retention_mode="keep")
    candidate = TreeFolder("r", mine.removeprefix("/a/"))
    existing = TreeFolder("r", theirs.removeprefix("/a/"), tree_id="t")
    with pytest.raises(TreeOverlapError):
        check_tree_attachment(candidate, [existing])
