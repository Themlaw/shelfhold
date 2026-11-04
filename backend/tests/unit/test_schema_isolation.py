"""The Core schema is infrastructure: it never reaches into a business context (PLAN-M1a A17)."""

import ast
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[2] / "src" / "shelfhold" / "infrastructure" / "schema"
CONTEXTS = {
    "catalog", "acquisition", "library", "export", "identity", "integrations",
    "jobs", "events", "plugin_host", "interfaces",
}  # fmt: skip


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.req("ENF-QUAL-002")
def test_schema_modules_import_no_business_context() -> None:
    modules = sorted(SCHEMA.glob("*.py"))
    assert {m.name for m in modules} >= {
        "_base.py",
        "platform.py",
        "identity.py",
        "coherence.py",
        "catalog.py",
        "acquisition.py",
    }
    for module in modules:
        for name in _imports(module):
            parts = name.split(".")
            assert not (parts[0] == "shelfhold" and len(parts) > 1 and parts[1] in CONTEXTS), (
                module.name,
                name,
            )


@pytest.mark.req("ENF-QUAL-002")
def test_every_table_lives_on_the_single_metadata() -> None:
    from shelfhold.infrastructure.schema import (
        acquisition,
        catalog,
        coherence,
        identity,
        metadata,
        platform,
    )

    for module in (acquisition, catalog, coherence, identity, platform):
        tables = [v for v in vars(module).values() if hasattr(v, "metadata") and hasattr(v, "c")]
        assert tables
        assert all(t.metadata is metadata for t in tables)
