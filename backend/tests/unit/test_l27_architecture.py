"""M3 L27 (EF-BIB-054 (4)): one function compares an upstream reference with a tombstone.

`TombstoneService.check` is the only place that follows the aliases and the spellings of a series
or a Unit reference. A door (follow, discovery, acquisition, tree import, import of a folder,
`migrate_ref`) that compared a reference with the membership index itself, or that built a
reference and never asked `check`, would let an erased series back under another form. This test
reads the production code and fails on either."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.req("EF-SRC-022", "EF-BIB-054")

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
TOMBSTONES = SRC / "library" / "tombstones"
INDEX_QUESTIONS = {"contains", "contains_values", "contains_folded", "series_refs"}
GATE_QUESTIONS = {"check", "check_refs", "screen_refs", "screen_migration", "series_blocked",
                  "variant_blocked"}  # fmt: skip
REFERENCES = {"SeriesBindingRef", "VariantRef"}
# They build references to **record** (a purge lays the tombstone) or to describe them: no door.
RECORDERS = {
    "export/application/purge.py",
}


def modules() -> list[tuple[str, ast.Module]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        if TOMBSTONES in path.parents:
            continue
        found.append((str(path.relative_to(SRC)), ast.parse(path.read_text(encoding="utf-8"))))
    return found


# a door may hand the question to its caller as a callable; the caller is checked below
CALLBACKS = {"tombstoned"}
HANDED_OVER = {"ops/acquire.py": "tombstoned"}  # who answers the callback of `acquire.prepare`


def called(tree: ast.Module) -> set[str]:
    names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    names |= {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in CALLBACKS
    }  # fmt: skip
    return names


def builds(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call) and getattr(node.func, "id", None) in REFERENCES
        for node in ast.walk(tree)
    )


def test_no_door_asks_the_membership_index_itself() -> None:
    offenders = []
    for name, tree in modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in INDEX_QUESTIONS
                and "index" in ast.unparse(node.func.value).lower()
            ):  # fmt: skip
                offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"compared with the index, not through `check`: {offenders}"


def test_every_module_that_builds_a_reference_asks_the_central_check() -> None:
    offenders = [
        name
        for name, tree in modules()
        if builds(tree)
        and name not in RECORDERS
        and not called(tree) & (GATE_QUESTIONS | CALLBACKS)
    ]
    assert not offenders, f"builds a series or Unit reference and never asks `check`: {offenders}"


def test_the_callback_of_a_door_is_answered_by_the_central_check() -> None:
    answered = {name: tree for name, tree in modules() if name in HANDED_OVER}
    assert set(answered) == set(HANDED_OVER)
    for name, tree in answered.items():
        lambdas = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.keyword) and n.arg == HANDED_OVER[name]
        ]  # fmt: skip
        assert lambdas, name
        for keyword in lambdas:
            assert "check" in {
                c.func.attr for c in ast.walk(keyword.value)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }, name  # fmt: skip


def test_the_recorders_really_never_compare() -> None:
    """The allow-list stays honest: a recorder that starts to ask `check` leaves it."""
    for name, tree in modules():
        if name in RECORDERS:
            assert builds(tree), f"{name} no longer builds a reference: remove it from RECORDERS"


def test_the_service_routes_references_through_one_function() -> None:
    service = ast.parse((TOMBSTONES / "service.py").read_text(encoding="utf-8"))
    klass = next(
        n for n in ast.walk(service) if isinstance(n, ast.ClassDef) and n.name == "TombstoneService"
    )
    check = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "check")
    assert "_check_reference" in {
        n.func.attr for n in ast.walk(check)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }  # fmt: skip
