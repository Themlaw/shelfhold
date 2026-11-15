"""The family shelf (a ceiling below 18+) is defined once: `export/domain/bindings.py`.

The barrier of the parental control rests on that rule; a second copy that drifts is a leak
(M3 closure review M6). The scan is by syntax: outside the definition, no comparison with the
literal 18 and no constant bound to 18 whose name speaks of the adult age or of a ceiling. Use
`is_family`, `family_clause` and `adult_clause`."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
DEFINITION = "export/domain/bindings.py"
# `X_MIN_AGE`: the age tier that X implies, not a shelf rule.
OTHER_MEANINGS = {"sdk/age.py"}
ADULT_NAMES = ("ADULT", "ADULT_FROM", "ADULT_MIN_AGE", "FAMILY")

pytestmark = pytest.mark.req("EF-USR-006", "ENF-SEC-010")


def _is_18(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 18 and not isinstance(node.value, bool)


def second_definitions(source: str) -> list[int]:
    """Lines that compare something with 18 or bind an adult-age constant to 18."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare) and any(_is_18(c) for c in [node.left, *node.comparators]):
            lines.append(node.lineno)
        elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id.upper() for t in targets if isinstance(t, ast.Name)]
            if _is_18(node.value) and any(a in n for n in names for a in ADULT_NAMES):
                lines.append(node.lineno)
    return lines


def test_the_family_shelf_rule_is_written_once() -> None:
    found = [
        f"{relative}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        if (relative := path.relative_to(SRC).as_posix()) != DEFINITION
        and relative not in OTHER_MEANINGS
        for line in second_definitions(path.read_text("utf-8"))
    ]
    assert not found, f"a second definition of the family shelf (use `is_family`): {found}"


def test_the_scan_sees_a_second_definition() -> None:
    assert second_definitions("ok = ceiling < 18\n") == [1]
    assert second_definitions("ADULT_FROM = 18\n") == [1]
    assert second_definitions("limit = 17\nx = ceiling < ADULT_MIN_AGE\n") == []
