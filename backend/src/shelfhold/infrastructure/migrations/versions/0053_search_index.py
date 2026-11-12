"""Full-text search tables (PLAN-M3 L17, EF-BIB-010, D7).

Two FTS5 virtual tables, a projection of the catalog that `shelfhold search rebuild` makes again:
`search_fts` (`unicode61`, diacritics folded) and `search_trigram` (substrings of CJK titles). A
SQLite without FTS5, or without the trigram tokenizer (older than 3.34), does not fail the
migration: the table it cannot make is left out, search falls back on `LIKE` and `status` says so.
The tables start empty; the next start (or `search rebuild`) fills them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0047"
branch_labels = None
depends_on = None

_COLUMNS = "entity_type UNINDEXED, entity_id UNINDEXED, digest UNINDEXED, title, names"
_TABLES = {
    "search_fts": "unicode61 remove_diacritics 2",
    "search_trigram": "trigram",
}


def upgrade() -> None:
    connection = op.get_bind()
    for table, tokenizer in _TABLES.items():
        try:
            with connection.begin_nested():
                connection.execute(
                    sa.text(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
                        f"{_COLUMNS}, tokenize = '{tokenizer}')"
                    )
                )
        except sa.exc.DBAPIError:
            pass  # no FTS5 or no such tokenizer here: `LIKE` instead (D7)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")
