"""M3 L08 (EF-USR-013, review of #218, M2): every writer of a column that names a user without a
foreign key reads the user first, in the transaction that writes (`still_a_user`).

A deletion un-names these columns (`SqlIdentityStore._unname`); a writer that authenticated its
user earlier and writes after the deletion would put the id back for ever. The scan reads the
whole package: a `values(...)` or `update(...)` that sets `decided_by` or `requester_user_id`, or
a dict that carries a `by` (the pause of the queue), must sit in a function that calls
`still_a_user`, or be listed below with its reason. A new writer without the guard fails here.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
COLUMNS = {"decided_by", "requester_user_id"}
GUARD = "still_a_user"

EXEMPT = {
    "shelfhold/identity/adapters/store.py::SqlIdentityStore._unname":
        "the deletion itself: it clears these columns",
    "shelfhold/acquisition/application/requests.py::RequestService._insert":
        "`request.requester_user_id` has a foreign key (ON DELETE CASCADE): the database refuses",
    "shelfhold/acquisition/application/requests.py::RequestService._revert":
        "writes `decided_by = NULL`: no user is named",
    "shelfhold/library/tombstones/values.py::_hashes":
        "a `by` that is the version of a hash, not a person",
}  # fmt: skip


def _writers() -> dict[str, bool]:
    """Each writer found (module::function) and whether its function calls the guard."""
    found: dict[str, bool] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name = path.relative_to(SRC).as_posix()

        def visit(
            node: ast.AST, stack: list[str], function: ast.AST | None, name: str = name
        ) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                stack, function = [*stack, node.name], node
            elif isinstance(node, ast.ClassDef):
                stack = [*stack, node.name]
            writes = (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") in ("values", "update")
                and any(k.arg in COLUMNS for k in node.keywords)
            ) or (
                isinstance(node, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == "by" for k in node.keys)
            )
            if writes and function is not None:
                guarded = any(
                    isinstance(c, ast.Call) and getattr(c.func, "id", "") == GUARD
                    for c in ast.walk(function)
                )
                key = f"{name}::{'.'.join(stack)}"
                found[key] = found.get(key, True) and guarded
            for child in ast.iter_child_nodes(node):
                visit(child, stack, function, name)

        visit(tree, [], None)
    return found


@pytest.mark.req("EF-USR-013")
def test_every_writer_of_a_user_column_without_a_foreign_key_reads_the_user_first() -> None:
    writers = _writers()
    assert len(writers) >= 8  # the scan sees the real writers
    unguarded = sorted(w for w, guarded in writers.items() if not guarded and w not in EXEMPT)
    assert unguarded == [], f"call still_a_user in the transaction that writes: {unguarded}"
    assert sorted(set(EXEMPT) - set(writers)) == []  # an exemption whose writer is gone is stale
    for guarded_writer in (
        "shelfhold/acquisition/application/acquire.py::NativeAcquirer._open_request",
        "shelfhold/acquisition/application/follow_request.py::FollowRequests._claim",
        "shelfhold/acquisition/application/requests.py::RequestService._claim",
        "shelfhold/acquisition/application/requests.py::RequestService._move",
        "shelfhold/jobs/control.py::QueueControl.pause",
    ):
        assert writers[guarded_writer] is True, guarded_writer


@pytest.mark.req("EF-USR-013")
def test_the_event_bus_reads_the_actor_and_the_declared_user_fields_before_it_writes() -> None:
    tree = ast.parse((SRC / "shelfhold/events/bus.py").read_text(encoding="utf-8"))
    (publish,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "publish"
    ]
    calls = [c for c in ast.walk(publish) if isinstance(c, ast.Call)]
    assert sum(getattr(c.func, "id", "") == GUARD for c in calls) >= 2  # the actor, the fields
    assert any(
        isinstance(n, ast.Name) and n.id == "USER_FIELDS" for n in ast.walk(publish)
    )  # the declared fields of the type
