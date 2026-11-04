"""ENF-I18N-001: user-facing strings live in the French catalog, not in the code."""

import ast
import re
from pathlib import Path

import pytest

from shelfhold.i18n import load_catalog, t
from shelfhold.interfaces.http_api.catalog import all_error_codes

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
USER_FACING = [SRC / "main.py", *sorted((SRC / "interfaces").rglob("*.py"))]


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _all_py() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py"))


@pytest.mark.req("ENF-I18N-001")
def test_ui_strings_extracted_no_hardcoded_text() -> None:
    catalog = load_catalog("fr")
    assert catalog, "the French catalog must not be empty"

    hardcoded: list[str] = []
    for path in USER_FACING:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and (re.search(r"\s", node.value) or re.search(r"[^\x00-\x7f]", node.value))
            ):
                hardcoded.append(f"{path.relative_to(SRC)}:{node.lineno}: {node.value!r}")
    assert not hardcoded, "hardcoded text, use t():\n" + "\n".join(hardcoded)

    used: set[str] = set()
    for path in _all_py():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                used.add(str(node.args[0].value))
    # Error texts are looked up by code (EF-UX-003): the declarations are the usages.
    used |= {key for spec in all_error_codes() for key in (spec.title_key, spec.action_key)}
    assert used - set(catalog) == set(), "keys missing from the catalog"
    assert set(catalog) - used == set(), "catalog keys never used"


@pytest.mark.req("ENF-I18N-001")
def test_catalog_is_french_and_interpolates() -> None:
    assert t("migrate.up_to_date") == load_catalog("fr")["migrate.up_to_date"]
    assert "0042" in t("migrate.applied", count=1, revision="0042")


@pytest.mark.req("ENF-I18N-001")
def test_unknown_key_is_reported_not_silently_empty() -> None:
    with pytest.raises(KeyError):
        t("no.such.key")
