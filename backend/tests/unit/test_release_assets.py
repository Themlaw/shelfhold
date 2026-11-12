"""`schema.sql` and the data dictionary, release assets (EF-INT-027).

Both files are generated, never written by hand: `schema.sql` from the Alembic migrations applied
to an empty database, the dictionary from the migrated database plus the descriptions file. A
test fails when a table or a column has no description, and when the workflow forgets an asset.
"""

import sqlite3
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from shelfhold.config import Settings, load_settings
from shelfhold.infrastructure.migrate import migrate

ROOT = Path(__file__).resolve().parents[3]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
SCRIPT = ROOT / ".github" / "scripts" / "release-assets.sh"
ASSETS = ("schema.sql", "data-dictionary.md")


def _migrated_catalog(workdir: Path) -> dict[str, list[str]]:
    """Table name -> column names of the schema the migrations produce, on an empty database."""
    settings = load_settings(
        {"SHELFHOLD_DATA_DIR": str(workdir / "data"), "SHELFHOLD_STORE_DIR": str(workdir / "store")}
    ).values
    migrate(settings)
    connection = sqlite3.connect(settings.database_path)
    try:
        tables = [
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                "AND name NOT GLOB 'search_fts*' AND name NOT GLOB 'search_trigram*' ORDER BY name"
            )
        ]
        return {
            t: [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{t}")')]
            for t in tables
        }
    finally:
        connection.close()


def _internal(name: str) -> bool:
    return name.startswith(("sqlite_", "search_fts", "search_trigram"))  # FTS5: a projection


def _catalog(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY 1, 2")
    return [(t, n, tbl, sql) for t, n, tbl, sql in rows if sql and not _internal(n)]


@pytest.fixture
def generated(tmp_path: Path) -> dict[str, str]:
    from shelfhold.docgen.release_assets import write_assets

    out = tmp_path / "assets"
    write_assets(out)
    return {name: (out / name).read_text(encoding="utf-8") for name in ASSETS}


@pytest.mark.req("EF-INT-027")
def test_release_assets_include_schema_sql_and_data_dictionary(
    tmp_path: Path, generated: dict[str, str]
) -> None:
    # the generated files exist and say what they are
    assert "CREATE TABLE" in generated["schema.sql"]
    assert "## Tables" in generated["data-dictionary.md"]
    # the script that builds them is the one the workflow runs ...
    assert SCRIPT.is_file()
    script = SCRIPT.read_text(encoding="utf-8")
    assert all(name in script for name in ASSETS)
    # ... and the workflow attaches both files to the release
    workflow: dict[str, Any] = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    generate = workflow["jobs"]["schema-assets"]
    assert "release-assets.sh" in "\n".join(str(step.get("run", "")) for step in generate["steps"])
    assert generate["permissions"] == {"contents": "read"}  # generates, never uploads
    # the single job with `contents: write` attaches them, with the other proofs
    job = workflow["jobs"]["release-proofs"]
    assert "schema-assets" in job["needs"]
    steps = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "gh release upload" in steps
    assert all(name in steps for name in ASSETS)
    assert job["permissions"]["contents"] == "write"


@pytest.mark.req("EF-INT-027")
def test_schema_sql_recreates_the_migrated_schema_with_sqlite_alone(
    settings: Settings, generated: dict[str, str]
) -> None:
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    migrated = sqlite3.connect(settings.database_path)
    rebuilt = sqlite3.connect(":memory:")
    try:
        rebuilt.executescript(generated["schema.sql"])  # `sqlite3` alone, nothing of Shelfhold
        from shelfhold.docgen.release_assets import normalize_ddl

        def same(connection: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
            return [(t, n, tbl, normalize_ddl(sql)) for t, n, tbl, sql in _catalog(connection)]

        assert same(rebuilt) == same(migrated)  # tables, indexes, views, triggers
        for table in (name for kind, name, _tbl, _sql in _catalog(migrated) if kind == "table"):
            # the numbering of constraints follows their order, which is not part of the schema
            for pragma, skip in (("table_xinfo", 0), ("foreign_key_list", 2), ("index_list", 1)):
                query = f'PRAGMA {pragma}("{table}")'
                assert sorted(r[skip:] for r in rebuilt.execute(query)) == sorted(
                    r[skip:] for r in migrated.execute(query)
                ), (table, pragma)
        assert rebuilt.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        rebuilt.close()
        migrated.close()


@pytest.mark.req("EF-INT-027")
def test_schema_sql_is_reproducible(tmp_path: Path) -> None:
    from shelfhold.docgen.release_assets import write_assets

    write_assets(tmp_path / "a")
    write_assets(tmp_path / "b")
    for name in ASSETS:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


@pytest.mark.req("EF-INT-027")
def test_every_table_and_column_has_a_description(tmp_path: Path) -> None:
    from shelfhold.docgen.release_assets import load_descriptions

    catalog = _migrated_catalog(tmp_path)
    descriptions = load_descriptions()
    missing = [
        f"{table}.{column}" if column else table
        for table, columns in catalog.items()
        for column in (None, *columns)
        if not descriptions.describe(table, column)
    ]
    assert missing == []


@pytest.mark.req("EF-INT-027")
def test_no_description_names_a_table_or_column_that_does_not_exist(tmp_path: Path) -> None:
    from shelfhold.docgen.release_assets import DESCRIPTIONS_FILE

    catalog = _migrated_catalog(tmp_path)
    raw = tomllib.loads(DESCRIPTIONS_FILE.read_text(encoding="utf-8"))
    stale = [f"tables.{t}" for t in raw["tables"] if t not in catalog]
    stale += [
        f"tables.{t}.columns.{c}"
        for t, entry in raw["tables"].items()
        if t in catalog
        for c in entry.get("columns", {})
        if c not in catalog[t]
    ]
    assert stale == []


@pytest.mark.req("EF-INT-027")
def test_dictionary_lists_sentinels_formats_and_their_versions(
    generated: dict[str, str],
) -> None:
    from shelfhold.docgen.release_assets import format_versions

    text = generated["data-dictionary.md"]
    assert "## Sentinel values" in text
    assert "## On-disk formats" in text
    for name, version in format_versions().items():
        assert f"`{name}`" in text
        assert version in text
    assert "`und`" in text  # the language sentinel
    assert "`'full', 'partial'`" in text  # allowed values come from the CHECK constraints


@pytest.mark.req("EF-INT-027")
def test_a_column_without_description_fails_the_generation(tmp_path: Path) -> None:
    from shelfhold.docgen.release_assets import (
        Descriptions,
        MissingDescriptionError,
        write_assets,
    )

    with pytest.raises(MissingDescriptionError, match=r"user\.username"):
        write_assets(tmp_path / "out", descriptions=Descriptions(tables={}, common={}))


@pytest.mark.req("EF-INT-027")
def test_normalize_ddl_leaves_a_statement_without_a_body_alone() -> None:
    from shelfhold.docgen.release_assets import normalize_ddl

    for sql in (
        "CREATE TABLE t (a INTEGER)",
        "CREATE INDEX i ON t (a)",
        "CREATE VIEW v AS SELECT 1",
    ):
        assert normalize_ddl(sql) == sql


@pytest.mark.req("EF-INT-027")
def test_normalize_ddl_sorts_constraints_and_keeps_a_multiline_check_whole() -> None:
    from shelfhold.docgen.release_assets import normalize_ddl

    check = "CONSTRAINT ck_b CHECK (a IN ('x, y',\n\t'z')\n\tAND b > 0)"
    plain = "CONSTRAINT ck_a CHECK (a > 0)"
    head = "CREATE TABLE t (\n\ta INTEGER, \n\tb INTEGER, \n\t"
    first = f"{head}{check}, \n\t{plain}\n)"
    second = f"{head}{plain}, \n\t{check}\n)"
    assert normalize_ddl(first) == normalize_ddl(second)
    out = normalize_ddl(first)
    assert check in out  # not split at the comma inside the quotes or at the line break
    assert out.index("ck_a") < out.index("ck_b")
    assert out.index("a INTEGER") < out.index("b INTEGER") < out.index("ck_a")
