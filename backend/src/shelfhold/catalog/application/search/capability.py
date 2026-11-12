"""What this SQLite and this database can do for search (D7).

FTS5 and the trigram tokenizer (SQLite 3.34 or later) are checked, not assumed: at the start, in
`status` and at every `rebuild`. Without them search falls back on `LIKE`, signalled and never a
failure.
"""

from dataclasses import dataclass

import sqlalchemy as sa

from shelfhold.catalog.application.search.tables import fts_tables

__all__ = ["SearchCapability", "ensure_tables", "search_capability", "tokenizer_available"]


@dataclass(frozen=True)
class SearchCapability:
    fts5: bool  # the word table is there and answers
    trigram: bool  # the trigram table is there and answers

    @property
    def mode(self) -> str:
        return "fts5" if self.fts5 else "like"

    @property
    def degraded(self) -> bool:
        return not (self.fts5 and self.trigram)


def _exists(connection: sa.Connection, table: str) -> bool:
    return (
        connection.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": table}
        ).first()
        is not None
    )


def tokenizer_available(connection: sa.Connection, table: str) -> bool:
    """Whether this SQLite can create `table` (a probe in the temporary schema, dropped)."""
    tokenizer = "trigram" if table == fts_tables.TRIGRAM_TABLE else "unicode61"
    try:
        with connection.begin_nested():
            probe = (
                f"CREATE VIRTUAL TABLE temp.shelfhold_probe USING fts5(x, tokenize='{tokenizer}')"
            )
            connection.execute(sa.text(probe))
            connection.execute(sa.text("DROP TABLE temp.shelfhold_probe"))
    except sa.exc.DBAPIError:
        return False
    return True


def search_capability(connection: sa.Connection) -> SearchCapability:
    def works(table: str) -> bool:
        if not _exists(connection, table):
            return False
        try:
            connection.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {table} MATCH '\"x\"'"))  # noqa: S608
        except sa.exc.DBAPIError:
            return False
        return True

    return SearchCapability(
        fts5=works(fts_tables.FTS_TABLE), trigram=works(fts_tables.TRIGRAM_TABLE)
    )


def ensure_tables(connection: sa.Connection) -> SearchCapability:
    """Lay down the tables this SQLite can have (an instance migrated before FTS5 was there)."""
    for table in (fts_tables.FTS_TABLE, fts_tables.TRIGRAM_TABLE):
        if not _exists(connection, table) and tokenizer_available(connection, table):
            connection.execute(sa.text(fts_tables.create_statement(table)))
    return search_capability(connection)
