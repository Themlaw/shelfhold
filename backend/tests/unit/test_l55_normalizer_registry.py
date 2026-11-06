"""L55 (ENF-OPS-021, ENF-SEC-009): number@2 is in the code, nothing is loaded from outside."""

import ast
from pathlib import Path

import pytest

from shelfhold.catalog.public import default_registry
from shelfhold.library.tombstones import CITABLE_NORMALIZERS, missing_citable

pytestmark = pytest.mark.req("ENF-OPS-021")

SOURCE = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def test_number_v2_is_in_the_code_and_citable() -> None:
    registry = default_registry()
    assert str(registry.current("number")) == "number@2"
    assert "number@2" in CITABLE_NORMALIZERS and missing_citable(registry) == []


@pytest.mark.parametrize(
    ("raw", "v1", "v2"),
    [
        ("1,500", "1.5", "1500"),
        ("12,345,678", "12.345", "12345678"),
        ("4,25", "4.25", "4.25"),  # a decimal comma stays one
        ("1,5000", "1.5", "1.5"),
        ("Ch.7", "7", "7"),
    ],
)
def test_number_v2_reads_only_three_digit_groups_as_thousands(raw: str, v1: str, v2: str) -> None:
    from shelfhold.catalog.domain.numbering import parse_number

    registry = default_registry()
    assert parse_number(raw).norm == v1
    assert registry.normalize("number", raw).value == v2


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
