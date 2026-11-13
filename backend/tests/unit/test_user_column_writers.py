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
    "shelfhold/ops/sheet.py::SheetOps.set_upstream_status":
        "a `by` that says where the status came from (`manual`), not a person",
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


# -- the audit: `actor_id` is written through `live_actor` (review of #218, the open point) --------

ACTOR_EXEMPT = {
    "shelfhold/identity/adapters/store.py::SqlIdentityStore.delete_user":
        "the deletion: it writes the token",
    "shelfhold/identity/adapters/store.py::SqlIdentityStore._unname":
        "the deletion: it writes the token",
    "shelfhold/events/bus.py::EventBus.publish":
        "the actor is read by `still_a_user` first (test above)",
    "shelfhold/library/tombstones/service.py::TombstoneService.accept_lost_keyring":
        "`principal.actor_id`: a `session:<id>` label, never a user id",
    "shelfhold/infrastructure/idempotency_store.py::IdempotencyStore.begin":
        "`principal.actor_id`: a label; the line is erased with the user",
}  # fmt: skip


def _actor_writers() -> dict[str, bool]:
    """Every `values(actor_id=X)` of the package and whether X is safe: nothing, a literal, a label
    (`principal.actor_id`, `.actor`) or a call to `live_actor`."""
    found: dict[str, bool] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name = path.relative_to(SRC).as_posix()

        def visit(node: ast.AST, stack: list[str], name: str = name) -> None:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                stack = [*stack, node.name]
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "values":
                for k in node.keywords:
                    if k.arg != "actor_id":
                        continue
                    v = k.value
                    safe = (
                        isinstance(v, ast.Constant | ast.JoinedStr)
                        or (isinstance(v, ast.Attribute) and v.attr in ("actor_id", "actor"))
                        or (isinstance(v, ast.IfExp) and "actor_id" in ast.unparse(v))
                        or (isinstance(v, ast.Call) and getattr(v.func, "id", "") == "live_actor")
                    )
                    key = f"{name}::{'.'.join(stack)}"
                    found[key] = found.get(key, True) and safe
            for child in ast.iter_child_nodes(node):
                visit(child, stack)

        visit(tree, [])
    return found


@pytest.mark.req("EF-USR-013")
def test_every_audit_writer_names_its_actor_through_the_guard_or_a_label() -> None:
    writers = _actor_writers()
    assert len(writers) >= 15  # the scan sees the real writers
    unsafe = sorted(w for w, safe in writers.items() if not safe and w not in ACTOR_EXEMPT)
    assert unsafe == [], f"write the actor through live_actor(...): {unsafe}"
    assert sorted(set(ACTOR_EXEMPT) - set(writers)) == []  # a stale exemption
    for guarded in (
        "shelfhold/identity/adapters/store.py::audit_insert",  # the identity service's door
        "shelfhold/headless/records.py::insert_audit",
    ):
        assert writers[guarded] is True, guarded
