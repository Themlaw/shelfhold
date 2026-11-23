"""The signer pin of an install from a signed index (M6 L08, EF-SRC-017 passe 8, GAP-8-13).

`plugin_install` keeps, for a plugin installed from an index, the repository's URL and the public
key of its signer: the triple (key, index, signer) an install is pinned to. A wheel the admin
gave by hand has neither — the pin is the admin's own act — and the two columns come and go
together (`pin_paired`): an install is pinned or it is not. Nothing existing is touched; the
columns start NULL for every installed plugin, which is the truth (none of them came from an
index yet). Reversible: the downgrade drops the columns, and the pin of a plugin installed from
an index is then simply forgotten — the files on disk say nothing about it.

Number: 0111 follows 0110 (the head of L07); 0109 stays with the 1.2.x hotfix.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

_TABLE = "plugin_install"
_COLUMNS = ("index_url", "signer_pubkey")
_CHECK = "ck_plugin_install_pin_paired"  # the name the schema's convention renders
_PIN_PAIRED = (
    '("index_url" IS NULL AND "signer_pubkey" IS NULL)'
    ' OR ("index_url" IS NOT NULL AND "signer_pubkey" IS NOT NULL)'
)


def _columns() -> set[str]:
    return {row[1] for row in op.get_bind().execute(sa.text(f"PRAGMA table_info({_TABLE})"))}


def _has_check() -> bool:
    sql = (
        op.get_bind()
        .execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": _TABLE},
        )
        .scalar()
    )
    return sql is not None and f"CONSTRAINT {_CHECK}" in sql


def _batch() -> Any:
    """Reflection hands `CURRENT_TIMESTAMP` back in a form SQLite re-renders with parentheses: the
    column is restated so the rebuilt table keeps the DDL `create_all` writes."""
    installed_at = sa.Column(
        "installed_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    return op.batch_alter_table(_TABLE, recreate="always", reflect_args=(installed_at,))


def upgrade() -> None:
    present = _columns()
    with _batch() as batch:
        for column in _COLUMNS:  # a database created since the schema gained them has them
            if column not in present:
                batch.add_column(sa.Column(column, sa.Text(), nullable=True))
        if not _has_check():
            batch.create_check_constraint(op.f(_CHECK), _PIN_PAIRED)


def downgrade() -> None:
    with _batch() as batch:
        if _has_check():
            batch.drop_constraint(op.f(_CHECK), type_="check")
        present = _columns()
        for column in _COLUMNS:
            if column in present:
                batch.drop_column(column)
