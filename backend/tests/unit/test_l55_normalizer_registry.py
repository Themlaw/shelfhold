"""L55 (ENF-OPS-021, ENF-SEC-009): nothing is loaded from outside."""

import ast
from pathlib import Path

import pytest

from shelfhold.catalog.public import default_registry
from shelfhold.library.tombstones import CITABLE_NORMALIZERS, missing_citable

pytestmark = pytest.mark.req("ENF-OPS-021")

SOURCE = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def test_production_registry_declares_no_number_version_beyond_the_stored_one() -> None:
    """The spec defines no `number@2`: the code ships `number@1` only, which is what ingestion
    writes, so `normalize migrate` has nothing to propose."""
    registry = default_registry()
    assert str(registry.current("number")) == "number@1"
    assert not any(n.endswith("@2") for n in CITABLE_NORMALIZERS)
    assert missing_citable(registry) == []


def test_no_production_module_loads_normalizers_from_entry_points() -> None:
    """A distribution on `sys.path` must never run code at startup (ENF-SEC-009)."""
    offenders = []
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "shelfhold.normalizers":
                offenders.append(str(path.relative_to(SOURCE)))
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "entry_points":
                groups = [k.value for k in node.keywords if k.arg == "group"]
                if any("normalizer" in ast.unparse(g) for g in groups):
                    offenders.append(str(path.relative_to(SOURCE)))
    assert offenders == []
