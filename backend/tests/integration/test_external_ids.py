"""L12 (EF-BIB-003): external identifiers, polymorphic, unique per entity type, append-only.

A value names one entity of a type; a renumbering by a provider adds the new value and marks the
old one `superseded` (`origin = legacy`): nothing is overwritten, nothing is deleted. The database
itself refuses any other UPDATE and every DELETE (triggers), and the repository offers neither.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.public import (
    ExternalId,
    ExternalIdConflictError,
    ExternalIdError,
    add_external_id,
    external_ids_of,
    holder_of,
    normalize_external_id,
    renumber_external_id,
)
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_schema_catalog import add_work, rejected

pytestmark = pytest.mark.req("EF-BIB-003")


@pytest.fixture
def migrated(settings: Settings) -> Iterator[Engine]:
    """The schema as the migrations build it (the triggers are theirs too)."""
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


def rows(engine: Engine) -> list[tuple[object, ...]]:
    table = catalog.external_id
    with engine.connect() as connection:
        return [
            tuple(r)
            for r in connection.execute(
                sa.select(
                    table.c.entity_type, table.c.entity_id, table.c.provider, table.c.value,
                    table.c.origin, table.c.state,
                ).order_by(table.c.entity_type, table.c.provider, table.c.value)
            )
        ]  # fmt: skip


def test_external_id_polymorphic_unique_per_entity_type(migrated: Engine) -> None:
    with migrated.begin() as connection:
        work, other = add_work(connection), add_work(connection)
        anilist = ExternalId("work", "anilist", "30013")
        assert add_external_id(connection, work, anilist, origin="provider") is True
        assert add_external_id(connection, work, anilist, origin="provider") is False  # same
        # the same value on another type is another identity: a Contributor may carry it
        person = new_ulid()
        assert add_external_id(
            connection, person, ExternalId("contributor", "anilist", "30013"), origin="provider"
        )
        with pytest.raises(ExternalIdConflictError) as conflict:
            add_external_id(connection, other, anilist, origin="source")
        assert conflict.value.holder_id == work
        assert holder_of(connection, anilist) == work
        assert [i.external.value for i in external_ids_of(connection, "work", other)] == []
    assert len(rows(migrated)) == 2


def test_external_id_renumbering_appends_never_overwrites(migrated: Engine) -> None:
    """A provider that moves from integer to alphanumeric identifiers (passe 8, W3)."""
    with migrated.begin() as connection:
        work = add_work(connection)
        add_external_id(
            connection, work, ExternalId("work", "mangaupdates", "123"), origin="source"
        )
        renumber_external_id(
            connection, work, ExternalId("work", "mangaupdates", "123"), "a1b2c3", origin="provider"
        )
        renumber_external_id(  # applying the table again changes nothing
            connection, work, ExternalId("work", "mangaupdates", "123"), "a1b2c3", origin="provider"
        )
        both = {i.external.value: i.state for i in external_ids_of(connection, "work", work)}
    assert both == {"123": "superseded", "a1b2c3": "current"}
    assert rows(migrated) == [
        ("work", work, "mangaupdates", "123", "legacy", "superseded"),
        ("work", work, "mangaupdates", "a1b2c3", "provider", "current"),
    ]
    with migrated.begin() as connection:  # the old value still names the Work
        assert holder_of(connection, ExternalId("work", "mangaupdates", "123")) == work


def test_external_id_update_or_delete_refused(migrated: Engine) -> None:
    """Append-only in the database: only the mark `current` -> `superseded` passes, once."""
    table = catalog.external_id
    with migrated.begin() as connection:
        work, other = add_work(connection), add_work(connection)
        add_external_id(connection, work, ExternalId("work", "anilist", "1"), origin="source")
    one = table.c.value == "1"
    for statement in (
        sa.update(table).where(one).values(value="2"),
        sa.update(table).where(one).values(entity_id=other),
        sa.update(table).where(one).values(provider="kitsu"),
        sa.update(table).where(one).values(origin="manual"),
        sa.delete(table).where(one),
        sa.insert(table)
        .prefix_with("OR REPLACE")
        .values(
            entity_type="work", entity_id=other, provider="anilist", value="1", origin="source"
        ),
    ):
        with migrated.begin() as connection:
            rejected(connection, statement, "external_id is append-only")
    with migrated.begin() as connection:
        connection.execute(sa.update(table).where(one).values(state="superseded", origin="legacy"))
    with migrated.begin() as connection:  # superseded is final
        rejected(connection, sa.update(table).where(one).values(state="current"), "append-only")
    assert rows(migrated) == [("work", work, "anilist", "1", "legacy", "superseded")]


@pytest.mark.parametrize(
    "sql",
    [
        # the review of #134: every other road to rewrite a row
        "INSERT INTO external_id (entity_type, entity_id, provider, value, origin) "
        "VALUES ('work', :other, 'anilist', '1', 'source') "
        "ON CONFLICT DO UPDATE SET entity_id = excluded.entity_id",
        "UPDATE external_id SET entity_id = :other WHERE rowid = 1",
        "UPDATE OR REPLACE external_id SET value = '2'",
        "REPLACE INTO external_id (entity_type, entity_id, provider, value, origin) "
        "VALUES ('work', :other, 'anilist', '1', 'source')",
        "DELETE FROM external_id",
    ],
)
def test_external_id_refuses_upserts_rowid_and_replace(migrated: Engine, sql: str) -> None:
    with migrated.begin() as connection:
        work, other = add_work(connection), add_work(connection)
        add_external_id(connection, work, ExternalId("work", "anilist", "1"), origin="source")
    with migrated.begin() as connection:
        statement = sa.text(sql)
        if ":other" in sql:
            statement = statement.bindparams(other=other)
        rejected(connection, statement, "external_id is append-only")
    assert rows(migrated) == [("work", work, "anilist", "1", "source", "current")]


def test_external_id_triggers_survive_vacuum_and_rename(migrated: Engine, tmp_path: Path) -> None:
    names = ["external_id_no_delete", "external_id_no_replace", "external_id_no_update"]
    copy = tmp_path / "copy.db"
    with closing(sqlite3.connect(migrated.url.database or "")) as db:
        db.execute("VACUUM INTO ?", (str(copy),))
    with closing(sqlite3.connect(copy)) as db:
        found = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'external_id' "
            "ORDER BY name"
        ).fetchall()
        assert [r[0] for r in found] == names
        db.execute("ALTER TABLE external_id RENAME TO renamed")
        found = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'renamed' "
            "ORDER BY name"
        ).fetchall()
        assert [r[0] for r in found] == names


@pytest.mark.parametrize(
    ("provider", "raw", "expected"),
    [
        ("isbn", "978-2-07-036024-6", ("isbn", "9782070360246")),
        ("isbn10", "2-07-036024-5", ("isbn", "9782070360246")),
        ("isbn", "0-8044-2957-x", ("isbn", "9780804429573")),
        ("anilist", " 30013 ", ("anilist", "30013")),
    ],
)
def test_isbn_normalized_to_isbn13(provider: str, raw: str, expected: tuple[str, str]) -> None:
    assert normalize_external_id("edition", provider, raw) == ExternalId("edition", *expected)


@pytest.mark.parametrize(
    ("entity_type", "provider", "raw"),
    [
        ("edition", "isbn", "978-2-07-036024-0"),  # bad check digit
        ("work", "isbn", "9782070360246"),  # an ISBN names an Edition or a Volume
        ("work", "Ani List", "1"),  # not a provider key
        ("work", "anilist", "  "),
        ("chapter", "anilist", "1"),
    ],
)
def test_external_id_refused_before_storage(entity_type: str, provider: str, raw: str) -> None:
    with pytest.raises(ExternalIdError):
        normalize_external_id(entity_type, provider, raw)
