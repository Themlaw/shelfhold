"""The family shelf (a ceiling below 18+) is defined once: `is_family_ceiling` in `sdk/age.py`
(`export/domain/bindings.py` only wraps it for SQL and for the export context).

The barrier of the parental control rests on that rule; a second copy that drifts is a leak
(M3 closure review M6, #265 review M-c and m-d). The scan is by syntax and refuses, outside the
two files above:

- a comparison with the literal 17 or 18, or with `X_MIN_AGE`, `UNRATED_FLOOR`, `ADULT_MIN_AGE`;
- a name or default argument about an age or a ceiling bound to the literal 18;
- a string that compares a ceiling with 17 or 18 (raw SQL).

It stays bypassable at the margin (`9 * 2`, a value passed through a function): it catches the
honest copy, a review catches the rest. Use `is_family_ceiling`, `family_clause`, `adult_clause`."""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
DEFINITIONS = {"sdk/age.py", "export/domain/bindings.py"}
THRESHOLDS = {17, 18}
BOUNDARY_NAMES = {"X_MIN_AGE", "UNRATED_FLOOR", "ADULT_MIN_AGE"}
# A name that speaks of an age or a ceiling: `LOG_N = 18` and `MAX_DIGITS = 18` are not the rule.
AGE_NAME = re.compile(r"adult|major|age|family|ceil|floor|from|min", re.IGNORECASE)
RAW_COMPARISON = re.compile(r"(max_age_rating|ceiling)\s*(<=|>=|<|>)\s*1[78]\b", re.IGNORECASE)

pytestmark = pytest.mark.req("EF-USR-006", "ENF-SEC-010")


def _threshold(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and not isinstance(node.value, bool)
        and node.value in THRESHOLDS
    )


def _is_18(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == 18 and node.value is not True


def _boundary(node: ast.AST) -> bool:
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
    return name in BOUNDARY_NAMES


def second_definitions(source: str) -> list[int]:
    """Lines that look like another copy of the family shelf rule."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_threshold(o) for o in operands) or any(_boundary(o) for o in operands):
                lines.append(node.lineno)
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if _is_18(node.value) and any(AGE_NAME.search(n) for n in names):
                lines.append(node.lineno)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            args = node.args
            pairs = [*zip(reversed(args.args), reversed(args.defaults), strict=False)]
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d]
            if any(_is_18(d) and AGE_NAME.search(a.arg) for a, d in pairs):
                lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if RAW_COMPARISON.search(node.value):
                lines.append(node.lineno)
    return sorted(set(lines))


def test_the_family_shelf_rule_is_written_once() -> None:
    found = [
        f"{relative}:{line}"
        for path in sorted(SRC.rglob("*.py"))
        if (relative := path.relative_to(SRC).as_posix()) not in DEFINITIONS
        for line in second_definitions(path.read_text("utf-8"))
    ]
    assert not found, f"a second definition of the family shelf (use `is_family_ceiling`): {found}"


@pytest.mark.parametrize(
    "copy",
    [
        "ok = ceiling < 18\n",
        "ok = ceiling <= 17\n",
        "ok = ceiling > 17\n",
        "ADULT_FROM = 18\n",
        "MAJORITY = 18\nok = ceiling < MAJORITY\n",
        "def family(ceiling, adult=18):\n    return ceiling < adult\n",
        'q = sa.text("SELECT id FROM shelf WHERE max_age_rating < 18")\n',
        "ok = ceiling < X_MIN_AGE\n",
        "ok = age < UNRATED_FLOOR\n",
    ],
)
def test_the_scan_sees_a_second_definition(copy: str) -> None:
    assert second_definitions(copy), copy


def test_the_scan_lets_the_one_source_through() -> None:
    assert second_definitions("limit = 17\nok = is_family_ceiling(ceiling)\n") == []
    assert second_definitions("age = AgeRating(X_MIN_AGE, x=True)\n") == []
