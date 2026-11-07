"""EF-USR-016: `activity_view(viewer, event)`, the one projection of an event for a person.

The table is written by hand (role x visibility x who owns it), never by calling the function.
An unknown role, an unknown visibility or an event of someone else's private request is closed.
"""

import ast
from itertools import product
from pathlib import Path

import pytest

import shelfhold.events as events_package
from shelfhold.events.activity import ActivityEvent, Viewer, activity_view

pytestmark = pytest.mark.req("EF-USR-016")

ME = "01HZZZZZZZZZZZZZZZZZZZZZZA"
OTHER_ADULT = "01HZZZZZZZZZZZZZZZZZZZZZZB"
A_CHILD = "01HZZZZZZZZZZZZZZZZZZZZZZC"
TITLE = "Un titre"


def event(visibility: str, *, owner: str | None, owner_role: str | None) -> ActivityEvent:
    return ActivityEvent(
        id=7, type="job.dead_lettered", visibility=visibility, payload={"job_kind": "download"},
        owner_id=owner, owner_role=owner_role, title=TITLE,
    )  # fmt: skip


# (viewer role, visibility, owner) -> "title" | "masked" | "hidden". The owner is one of:
# "self" (the viewer), "adult" (another adult), "child" (a child), "none" (no owner: ops).
EXPECTED = {
    ("admin", "ops", "none"): "title",
    ("admin", "household", "self"): "title",
    ("admin", "household", "adult"): "title",
    ("admin", "household", "child"): "title",
    ("admin", "private", "self"): "title",
    ("admin", "private", "adult"): "masked",
    ("admin", "private", "child"): "title",
    ("admin", "private", "none"): "masked",
    ("member", "ops", "none"): "hidden",
    ("member", "household", "self"): "title",
    ("member", "household", "adult"): "title",
    ("member", "household", "child"): "title",  # an adult counts as a parent (D19)
    ("member", "private", "self"): "title",
    ("member", "private", "adult"): "hidden",
    ("member", "private", "child"): "hidden",
    ("member", "private", "none"): "hidden",
    ("child", "ops", "none"): "hidden",
    ("child", "household", "self"): "title",
    ("child", "household", "adult"): "hidden",
    ("child", "household", "child"): "hidden",
    ("child", "private", "self"): "title",
    ("child", "private", "adult"): "hidden",
    ("child", "private", "child"): "hidden",
    ("child", "private", "none"): "hidden",
    # the integration role (webhooks, outbox: GAP-7-16) never carries a title
    ("integration", "ops", "none"): "untitled",
    ("integration", "household", "self"): "untitled",
    ("integration", "household", "adult"): "untitled",
    ("integration", "household", "child"): "untitled",
    ("integration", "private", "self"): "hidden",
    ("integration", "private", "adult"): "hidden",
    ("integration", "private", "child"): "hidden",
    ("integration", "private", "none"): "hidden",
    # an `ops` event is an operation: admin and integration only, whoever caused it
    ("admin", "ops", "self"): "title",
    ("admin", "ops", "adult"): "title",
    ("admin", "ops", "child"): "title",
    ("member", "ops", "self"): "hidden",
    ("member", "ops", "adult"): "hidden",
    ("member", "ops", "child"): "hidden",
    ("child", "ops", "self"): "hidden",
    ("child", "ops", "adult"): "hidden",
    ("child", "ops", "child"): "hidden",
    ("integration", "ops", "self"): "untitled",
    ("integration", "ops", "adult"): "untitled",
    ("integration", "ops", "child"): "untitled",
    # a household event with no owner: the adults of the house, never a child
    ("admin", "household", "none"): "title",
    ("member", "household", "none"): "title",
    ("child", "household", "none"): "hidden",
    ("integration", "household", "none"): "untitled",
}
OWNERS = {
    "self": (ME, "member"),
    "adult": (OTHER_ADULT, "member"),
    "child": (A_CHILD, "child"),
    "none": (None, None),
}
SELF_ROLE = {"admin": "admin", "member": "member", "child": "child", "integration": "integration"}


def case(role: str, visibility: str, owner: str) -> tuple[Viewer, ActivityEvent]:
    owner_id, owner_role = OWNERS[owner]
    if owner == "self":
        owner_role = SELF_ROLE[role]
    return Viewer(ME, role), event(visibility, owner=owner_id, owner_role=owner_role)


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_activity_view_truth_table() -> None:
    seen = set()
    for (role, visibility, owner), outcome in EXPECTED.items():
        viewer, ev = case(role, visibility, owner)
        item = activity_view(viewer, ev)
        seen.add((role, visibility, owner))
        if outcome == "hidden":
            assert item is None, (role, visibility, owner)
            continue
        assert item is not None, (role, visibility, owner)
        assert item.event_id == 7
        assert item.payload == {"job_kind": "download"}  # the operation stays visible
        if outcome == "title":
            assert (item.title, item.title_masked) == (TITLE, False)
        elif outcome == "masked":
            assert (item.title, item.title_masked) == (None, True)
        else:
            assert (item.title, item.title_masked) == (None, False)
    # the table is exhaustive on its three axes: nothing was left to the default
    every = set(
        product(
            ("admin", "member", "child", "integration"), ("ops", "household", "private"), OWNERS
        )
    )
    assert seen == every


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_admin_sees_masked_title_for_private_adult_request() -> None:
    viewer, ev = case("admin", "private", "adult")
    item = activity_view(viewer, ev)
    assert item is not None
    assert item.title is None and item.title_masked
    assert TITLE not in repr(item)  # not even in a log line
    assert item.payload  # the operation (job, error) is shown


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["ghost", "", "ADMIN", "root"])
def test_unknown_viewer_role_sees_nothing(role: str) -> None:
    for visibility in ("ops", "household", "private"):
        assert (
            activity_view(Viewer(ME, role), event(visibility, owner=ME, owner_role="member"))
            is None
        )


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
@pytest.mark.parametrize("visibility", ["public", "", "Private", "ops "])
def test_unknown_visibility_is_hidden_from_everyone(visibility: str) -> None:
    for role in ("admin", "member", "child", "integration"):
        assert (
            activity_view(Viewer(ME, role), event(visibility, owner=ME, owner_role="member"))
            is None
        )


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_an_owner_with_an_unknown_role_is_treated_as_an_adult() -> None:
    item = activity_view(
        Viewer(ME, "admin"), event("private", owner=OTHER_ADULT, owner_role="ghost")
    )
    assert item is not None and item.title_masked


@pytest.mark.req("EF-USR-016", "ENF-SEC-010")
def test_no_other_projection_of_an_event_exposes_a_title() -> None:
    """`activity_view` is the only place of the events package where an event meets a title."""
    root = Path(events_package.__file__).parent
    offenders = []
    for path in sorted(root.glob("*.py")):
        if path.name == "activity.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value == "title":
                offenders.append(f"{path.name}:{node.lineno}")
            if (
                isinstance(node, ast.Attribute | ast.Name)
                and getattr(node, "attr", getattr(node, "id", "")) == "title"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []
