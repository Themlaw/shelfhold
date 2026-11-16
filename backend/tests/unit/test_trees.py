"""EF-EXP-058, ADR-0052 T1: one tree, several indexers. When two folders are one tree, and when
they only overlap. Pure rules: no disk, the caller hands over what it saw."""

import pytest

from shelfhold.export.domain.bindings import TreeOverlapError
from shelfhold.export.domain.trees import (
    Relation,
    TreeFolder,
    TreePathError,
    check_no_symlink,
    check_tree_attachment,
    normalize_subpath,
    relation,
)

ROOT = "01JBZ3X5QH8M2N4P6R7S9T0V1R"
OTHER_ROOT = "01JBZ3X5QH8M2N4P6R7S9T0V2R"
TREE = "01JBZ3X5QH8M2N4P6R7S9T0V1T"


def folder(subpath: str, *, tree_id: str | None = None, root: str = ROOT, **extra: object) -> TreeFolder:
    return TreeFolder(root_id=root, subpath=subpath, tree_id=tree_id, **extra)  # type: ignore[arg-type]


DECLARED = folder("manga/Kavita", tree_id=TREE)


@pytest.mark.req("EF-EXP-058")
def test_tree_overlap_refused_unless_same_tree_per_adr() -> None:
    """The exact identity of a declared tree attaches, on the admin's explicit choice. Every other
    overlap stays refused with `target.tree_overlap`."""
    assert check_tree_attachment(folder("manga/Kavita"), [DECLARED], attach_to=TREE) == DECLARED
    assert check_tree_attachment(folder("manga/Other"), [DECLARED]) is None  # a new tree
    assert check_tree_attachment(folder("comics/Kavita"), [DECLARED]) is None
    assert check_tree_attachment(folder("manga/Kavita", root=OTHER_ROOT), [DECLARED]) is None
    with pytest.raises(TreeOverlapError) as refused:  # the same folder, no explicit attachment
        check_tree_attachment(folder("manga/Kavita"), [DECLARED])
    assert refused.value.code == "target.tree_overlap"
    with pytest.raises(TreeOverlapError):  # an explicit choice of another tree
        check_tree_attachment(folder("manga/Kavita"), [DECLARED], attach_to="01JBZ3X5QH8M2N4P6R7S9T0V9Z")
    with pytest.raises(TreeOverlapError):  # attaching to a tree that is not this folder
        check_tree_attachment(folder("manga/Other"), [DECLARED], attach_to=TREE)


@pytest.mark.req("EF-EXP-058")
@pytest.mark.parametrize(
    ("candidate", "how"),
    [
        ("manga/Kavita/Inner", Relation.INSIDE),
        ("manga", Relation.CONTAINS),
        ("", Relation.CONTAINS),
        ("manga/kavita", Relation.LOOKALIKE),  # case: never one tree, always refused
        ("MANGA/KAVITA", Relation.LOOKALIKE),
        ("manga/Kavita.", Relation.LOOKALIKE),  # a trailing dot is lax on some file systems
        ("manga/Kavita ", Relation.LOOKALIKE),
        ("manga/Kavitá", Relation.APART),  # another name: not an overlap
        ("manga/Kavita/Inner", Relation.INSIDE),
        ("manga/Kavi", Relation.APART),  # a string prefix is not a prefix of components
        ("manga/Kavitas", Relation.APART),
        ("mang", Relation.APART),
    ],
)
def test_tree_overlap_is_decided_by_components_and_never_by_string_prefix(
    candidate: str, how: Relation
) -> None:
    assert relation(folder(candidate), DECLARED) is how
    if how is Relation.APART:
        assert check_tree_attachment(folder(candidate), [DECLARED]) is None
    else:
        with pytest.raises(TreeOverlapError):
            check_tree_attachment(folder(candidate), [DECLARED], attach_to=TREE)


@pytest.mark.req("EF-EXP-058")
def test_overlap_of_a_folder_with_a_deposit_box_inside_or_around_a_tree() -> None:
    assert relation(folder("manga/Kavita/inbox"), DECLARED) is Relation.INSIDE
    assert relation(DECLARED, folder("manga/Kavita/inbox")) is Relation.CONTAINS


@pytest.mark.req("EF-EXP-058")
def test_same_inode_is_one_folder_and_an_ancestor_inode_is_an_overlap() -> None:
    mine = folder("alias/Kavita", identity=(7, 100), ancestry=frozenset({(7, 1), (7, 100)}))
    declared = folder("manga/Kavita", tree_id=TREE, identity=(7, 100), ancestry=frozenset({(7, 1), (7, 100)}))
    assert relation(mine, declared) is Relation.LINKED  # a bind mount, another path, one folder
    with pytest.raises(TreeOverlapError):
        check_tree_attachment(mine, [declared], attach_to=TREE)  # never an identity by inode
    parent = folder("other/area", identity=(7, 50), ancestry=frozenset({(7, 1), (7, 50)}))
    below = folder("x/y", identity=(7, 60), ancestry=frozenset({(7, 1), (7, 50), (7, 60)}))
    assert relation(below, parent) is Relation.LINKED
    assert relation(parent, below) is Relation.LINKED
    apart = folder("z", identity=(7, 70), ancestry=frozenset({(7, 1), (7, 70)}))
    assert relation(apart, parent) is Relation.APART


@pytest.mark.req("EF-EXP-058")
def test_a_tree_is_not_its_own_neighbour() -> None:
    assert check_tree_attachment(DECLARED, [DECLARED]) is None


@pytest.mark.req("EF-EXP-058")
@pytest.mark.parametrize(
    "raw",
    [
        "..", "../x", "a/../b", "a/..", "./a", "a/./b", "/abs", "a/", "a//b", "a\\b", "a\x00b",
        "a\nb", "a\u200bb", "//", "/",
    ],
)
def test_subpath_that_climbs_or_is_not_a_folder_name_is_refused(raw: str) -> None:
    with pytest.raises(TreePathError):
        normalize_subpath(raw)


@pytest.mark.req("EF-EXP-058")
@pytest.mark.parametrize("raw", ["", "a", "a/b", "Série/Ñ", "a b/c"])
def test_subpath_that_is_a_folder_name_is_kept_as_it_is(raw: str) -> None:
    assert normalize_subpath(raw) == raw


@pytest.mark.req("EF-EXP-058")
def test_subpath_too_long_is_refused() -> None:
    with pytest.raises(TreePathError):
        normalize_subpath("a/" * 3000 + "b")


@pytest.mark.req("EF-EXP-058")
def test_a_tree_folder_never_goes_through_a_symbolic_link() -> None:
    kinds = {"manga": "dir", "manga/Kavita": "symlink"}
    with pytest.raises(TreePathError):
        check_no_symlink("manga/Kavita/Series", kinds.get)
    with pytest.raises(TreePathError):
        check_no_symlink("manga/Kavita", kinds.get)
    check_no_symlink("manga/Other", kinds.get)
    check_no_symlink("", kinds.get)
    with pytest.raises(TreePathError):  # a link higher up
        check_no_symlink("a/b/c", {"a": "symlink"}.get)


@pytest.mark.req("EF-EXP-058")
def test_a_tree_folder_carries_a_normalized_subpath() -> None:
    with pytest.raises(TreePathError):
        folder("manga/../Kavita")
