"""Full-text search tables (FTS), declared by the M3 search lot. Only
`shelfhold.catalog.application.search` may import this module (contract `fts-sole-reader`).

Two virtual tables hold the same documents, one per tokenizer: `search_fts` (FTS5 `unicode61`,
diacritics folded, for words) and `search_trigram` (FTS5 `trigram`, for the middle of a CJK title).
They are a projection of the catalog, never a source: `rebuild` makes them again from the
database. They have no row of `unit_coverage`, no age and no profile: the parental filter is the
query's, never the index's. The migration (0047) lays the same DDL down; `ensure` repeats it for an
instance whose SQLite gained FTS5 after the migration.
"""

import sqlalchemy as sa

__all__ = [
    "COLUMNS",
    "FTS_TABLE",
    "TRIGRAM_TABLE",
    "create_statement",
    "fts",
    "trigram",
]

FTS_TABLE = "search_fts"
TRIGRAM_TABLE = "search_trigram"
COLUMNS = ("entity_type", "entity_id", "digest", "title", "names")
_TOKENIZERS = {
    FTS_TABLE: "unicode61 remove_diacritics 2",
    TRIGRAM_TABLE: "trigram",
}


def create_statement(table: str) -> str:
    """`CREATE VIRTUAL TABLE IF NOT EXISTS`: the three first columns are kept, not indexed."""
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
        "entity_type UNINDEXED, entity_id UNINDEXED, digest UNINDEXED, title, names, "
        f"tokenize = '{_TOKENIZERS[table]}')"
    )


def _view(name: str) -> sa.TableClause:
    return sa.table(name, *(sa.column(c) for c in COLUMNS))


fts = _view(FTS_TABLE)
trigram = _view(TRIGRAM_TABLE)
