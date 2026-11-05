"""EF-BIB-045: one function counts copies, and a contract says nobody else may."""

import ast
import configparser
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "shelfhold"
READER = "shelfhold/library/replica.py"
SCHEMA_PACKAGE = "shelfhold/infrastructure/schema/"


def replica_table_users() -> list[str]:
    """Modules that touch the `replica` table: any `storage.replica` / `from ... import replica`."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        if rel.startswith(SCHEMA_PACKAGE):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            by_attribute = isinstance(node, ast.Attribute) and node.attr == "replica"
            by_import = isinstance(node, ast.ImportFrom) and any(
                a.name == "replica" for a in node.names
            )
            if by_attribute or by_import:
                found.append(rel)
    return sorted(set(found))


@pytest.mark.req("EF-BIB-045")
def test_independent_copies_single_source_of_truth() -> None:
    assert replica_table_users() == [READER]  # no other module reads or writes the registry
    definers = [
        path.relative_to(SRC.parent).as_posix()
        for path in SRC.rglob("*.py")
        if "def independent_copies" in path.read_text(encoding="utf-8")
    ]
    assert definers == [READER]  # and there is one function that counts
    config = configparser.ConfigParser()
    config.read(BACKEND / "importlinter.ini", encoding="utf-8")
    contract = config["importlinter:contract:replica-single-reader"]
    assert contract["type"] == "forbidden"
    assert "shelfhold.infrastructure.schema.storage" in contract["forbidden_modules"]
    allowed = contract["ignore_imports"]
    assert "shelfhold.library.replica -> shelfhold.infrastructure.schema.storage" in allowed
    assert contract.get("unmatched_ignore_imports_alerting") != "none"
