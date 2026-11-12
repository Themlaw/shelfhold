"""Release assets: `schema.sql` and the data dictionary (EF-INT-027).

Both files are generated from the Alembic migrations applied to an empty database; neither is ever
written by hand. `schema.sql` is the catalog of that database (tables, indexes, views, triggers),
so that a database found years later reads and rebuilds with `sqlite3` alone. The dictionary adds
a description per table and per column (`schema_descriptions.toml`), the values that are
sentinels, and the formats Shelfhold writes on disk with their versions. A table or a column
without a description fails the generation.

    python -m shelfhold.docgen.release_assets <output directory>
"""

import argparse
import re
import sqlite3
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shelfhold.config import CONFIG_SCHEMA_VERSION, Settings, load_settings
from shelfhold.export.application.intents import INTENT_SCHEMA_VERSION
from shelfhold.export.domain.physical_roots import SENTINEL_SCHEMA_VERSION
from shelfhold.export.domain.provenance_mirror import MIRROR_SCHEMA_VERSION
from shelfhold.infrastructure.migrate import head_revision, migrate
from shelfhold.infrastructure.secrets.envelope import FORMAT_VERSION as ENVELOPE_FORMAT
from shelfhold.infrastructure.secrets.keyring import KEYRING_FORMAT
from shelfhold.library.domain.unit_json import UNIT_JSON_VERSION
from shelfhold.library.tombstones.journal import FORMAT as TOMBSTONE_JOURNAL_FORMAT

DESCRIPTIONS_FILE = Path(__file__).with_name("schema_descriptions.toml")
SCHEMA_FILE = "schema.sql"
DICTIONARY_FILE = "data-dictionary.md"

# Values that mean something other than their face value (02 section 3). Kept next to the
# generator on purpose: the dictionary must say it, and a test reads it back.
SENTINELS: tuple[tuple[str, str, str], ...] = (
    ("`''` (empty string)", "`edition`: `publisher`, `text_version`, `performance`, `packaging`",
     "A part of the Edition key that is not set. Never NULL: SQLite treats NULLs as distinct in "
     "a unique index, which would let duplicates in."),
    ("`''` (empty string)", "`unit.number_segment`, `unit.number_norm`",
     "No numbering segment, or an extra without a number (its place is `source_order`)."),
    ("`und`", "every `lang` column", "Language not known; the Unit waits in `language_unknown`."),
    ("`mul`, `multi`, `all`", "`sub_source.lang`, language filters",
     "Wildcards, never part of an Edition key."),
    ("`NULL`", "`shelf.max_age_rating`", "No age ceiling: an adult shelf."),
    ("`NULL`", "`work.reading_direction`, `user.theme`",
     "Use the kind's, or the instance's, default."),
    ("`NULL`", "`series_follow.follow_from_number`", "No watermark yet."),
    ("`NULL`", "`field_owner.pushed_at`", "Invalidated: to be pushed again."),
    ("`NULL`", "`content.unit_id`", "A file that carries several Units."),
    ("`NULL`", "`content.variant_id`", "A file adopted in place, with no Variant (`adopted_ref`)."),
    ("`NULL`", "`edition.shelf_id`", "Unshelved: never exported."),
    ("`NULL`", "`trash_item.physical_root_id`", "The bin of `/store`."),
    ("`NULL`", "`idempotency_key.response_status`", "The first request is still running."),
    ("`NULL`", "`unit_coverage.start_locator`, `end_locator`", "Bounds in the carrier unknown."),
)  # fmt: skip


class MissingDescriptionError(Exception):
    """A table or a column of the migrated schema has no description."""

    def __init__(self, names: Sequence[str]) -> None:
        super().__init__("no description for: " + ", ".join(names))
        self.names = tuple(names)


@dataclass(frozen=True)
class Descriptions:
    tables: Mapping[str, Any]
    common: Mapping[str, str]

    def describe(self, table: str, column: str | None = None) -> str | None:
        entry = self.tables.get(table, {})
        if column is None:
            text = entry.get("description")
        else:
            text = entry.get("columns", {}).get(column) or self.common.get(column)
        return text if isinstance(text, str) and text.strip() else None


def load_descriptions(path: Path = DESCRIPTIONS_FILE) -> Descriptions:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return Descriptions(tables=raw.get("tables", {}), common=raw.get("common", {}))


def format_versions() -> dict[str, str]:
    """Every format Shelfhold writes on disk, with the version this build writes."""
    return {
        "config.toml": str(CONFIG_SCHEMA_VERSION),
        "unit.json": str(UNIT_JSON_VERSION),
        "provenance mirror": str(MIRROR_SCHEMA_VERSION),
        "mount sentinel": str(SENTINEL_SCHEMA_VERSION),
        "tombstone journal": str(TOMBSTONE_JOURNAL_FORMAT),
        "keyring": str(KEYRING_FORMAT),
        "secret envelope": str(ENVELOPE_FORMAT),
        "file-operation intent": str(INTENT_SCHEMA_VERSION),
    }


_FORMAT_NOTES = {
    "config.toml": "`/data/config.toml`, `major.minor`; a newer major is refused.",
    "unit.json": "Next to each kept file; `major.minor`, a reader keeps what it does not know.",
    "provenance mirror": "`<root>/.shelfhold/manifest`, JSON lines per root, `major.minor`.",
    "mount sentinel": "Marker file of a mount, `major.minor`.",
    "tombstone journal": "`<root>/.shelfhold/tombstones`, append-only, one authenticated line per "
    "tombstone, identifiers encrypted.",
    "keyring": "Key file; only the registry of key ids is in the database.",
    "secret envelope": "Version, key id, nonce and ciphertext; part of the associated data.",
    "file-operation intent": "`fs_intent.intent_schema_version`; another version never replays.",
}


def _settings(workdir: Path) -> Settings:
    return load_settings(
        {"SHELFHOLD_DATA_DIR": str(workdir / "data"), "SHELFHOLD_STORE_DIR": str(workdir / "store")}
    ).values


def _migrated_database(workdir: Path) -> Path:
    settings = _settings(workdir)
    migrate(settings)  # an empty database, every migration applied in order
    return settings.database_path


def _tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' "
        "ESCAPE '\\' AND name NOT GLOB 'search_fts*' AND name NOT GLOB 'search_trigram*' "
        "ORDER BY name"
    )
    return [name for (name,) in rows]


def _columns(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    return list(connection.execute(f'PRAGMA table_info("{table}")'))


def _split_top_level(body: str) -> list[str]:
    """Split on the commas outside parentheses and quotes: a multi-line CHECK stays whole."""
    items: list[str] = []
    depth, quote, start = 0, "", 0
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(body[start:index])
            start = index + 1
    items.append(body[start:])
    return [item.strip() for item in items if item.strip()]


def normalize_ddl(sql: str) -> str:
    """The same statement with the table constraints in name order.

    The migrations rebuild tables in batch mode and the order of the constraints in the rebuilt
    statement varies from one run to the next (a set is iterated); the schema does not. Columns
    keep their order, which is the order of the data."""
    if not sql.lstrip().upper().startswith("CREATE TABLE") or "(\n" not in sql:
        return sql
    head, _, rest = sql.partition("(\n")
    body, _, tail = rest.rpartition("\n)")
    items = _split_top_level(body)
    columns = [i for i in items if not i.startswith("CONSTRAINT ")]
    constraints = sorted(i for i in items if i.startswith("CONSTRAINT "))
    return head + "(\n\t" + ",\n\t".join([*columns, *constraints]) + "\n)" + tail


def render_schema_sql(connection: sqlite3.Connection, revision: str) -> str:
    """The catalog of the database, in an order that `sqlite3` can replay."""
    rank = {"table": 0, "index": 1, "view": 2, "trigger": 3}
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "AND name NOT GLOB 'search_fts*' AND name NOT GLOB 'search_trigram*'"
    ).fetchall()
    rows.sort(key=lambda row: (rank[row[0]], row[1]))
    header = (
        "-- Shelfhold database schema, generated from the Alembic migrations applied to an empty\n"
        f"-- database (revision {revision}). Do not edit: it is rebuilt at every release.\n"
        "-- Replay with: sqlite3 shelfhold.db < schema.sql\n\n"
    )
    return header + "".join(f"{normalize_ddl(sql).rstrip()};\n\n" for _type, _name, sql in rows)


def _allowed_values(table_sql: str, column: str) -> str | None:
    match = re.search(rf'"{re.escape(column)}" IN \(([^)]*)\)', table_sql)
    return match.group(1) if match else None


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _render_table(
    connection: sqlite3.Connection, table: str, descriptions: Descriptions, missing: list[str]
) -> list[str]:
    description = descriptions.describe(table)
    if description is None:
        missing.append(table)
    (table_sql,) = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    # A column in a composite key as well as in its own: the single-column key names it, whatever
    # order SQLite lists them in (a table rebuilt by a migration lists them in no fixed order).
    foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    width = {
        fk_id: sum(1 for row in foreign_keys if row[0] == fk_id)
        for fk_id in {r[0] for r in foreign_keys}
    }
    references: dict[str, str] = {}
    for row in sorted(foreign_keys, key=lambda r: (width[r[0]], str(r[2]), str(r[4]))):
        references.setdefault(str(row[3]), f"{row[2]}.{row[4]}")
    lines = [f"### `{table}`", "", description or "", "",
             "| Column | Type | Null | Default | Key | Allowed values | Description |",
             "| --- | --- | --- | --- | --- | --- | --- |"]  # fmt: skip
    for column in _columns(connection, table):
        name = str(column["name"])
        text = descriptions.describe(table, name)
        if text is None:
            missing.append(f"{table}.{name}")
        key = ["PK"] if column["pk"] else []
        if name in references:
            key.append(f"FK {references[name]}")
        allowed = _allowed_values(str(table_sql), name)
        lines.append(
            "| " + " | ".join(
                _cell(cell)
                for cell in (
                    f"`{name}`",
                    str(column["type"] or ""),
                    "no" if column["notnull"] else "yes",
                    "" if column["dflt_value"] is None else f"`{column['dflt_value']}`",
                    ", ".join(key),
                    "" if allowed is None else f"`{allowed}`",
                    text or "",
                )
            ) + " |"
        )  # fmt: skip
    return [*lines, ""]


def render_data_dictionary(
    connection: sqlite3.Connection, revision: str, descriptions: Descriptions
) -> str:
    missing: list[str] = []
    out = [
        "# Shelfhold data dictionary",
        "",
        f"Generated from the database schema at revision `{revision}`. Do not edit: it is rebuilt "
        "at every release, with `schema.sql`. Instants are UTC; identifiers are ULIDs.",
        "",
        "## Tables",
        "",
    ]
    for table in _tables(connection):
        out += _render_table(connection, table, descriptions, missing)
    views = [
        name
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY 1"
        )
    ]
    if views:
        out += ["## Views", "", "Read models, recomputed on every read, never written.", ""]
        out += [f"- `{view}`" for view in views] + [""]
    if missing:
        raise MissingDescriptionError(missing)
    out += ["## Sentinel values", "",
            "| Value | Where | Meaning |", "| --- | --- | --- |"]  # fmt: skip
    out += [
        f"| {_cell(v)} | {_cell(where)} | {_cell(meaning)} |" for v, where, meaning in SENTINELS
    ]
    out += ["", "## On-disk formats", "",
            "| Format | Version written | Notes |", "| --- | --- | --- |"]  # fmt: skip
    for name, version in format_versions().items():
        out.append(f"| `{_cell(name)}` | `{version}` | {_cell(_FORMAT_NOTES[name])} |")
    return "\n".join(out) + "\n"


def write_assets(out_dir: Path, *, descriptions: Descriptions | None = None) -> tuple[Path, Path]:
    descriptions = descriptions if descriptions is not None else load_descriptions()
    with tempfile.TemporaryDirectory() as workdir:
        connection = sqlite3.connect(_migrated_database(Path(workdir)))
        try:
            revision = head_revision()
            schema = render_schema_sql(connection, revision)
            dictionary = render_data_dictionary(connection, revision, descriptions)
        finally:
            connection.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    schema_path, dictionary_path = out_dir / SCHEMA_FILE, out_dir / DICTIONARY_FILE
    schema_path.write_text(schema, encoding="utf-8")
    dictionary_path.write_text(dictionary, encoding="utf-8")
    return schema_path, dictionary_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate schema.sql and data-dictionary.md")
    parser.add_argument("output", type=Path, help="directory to write both files into")
    args = parser.parse_args(argv)
    try:
        written = write_assets(args.output)
    except MissingDescriptionError as error:
        print(error, file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
