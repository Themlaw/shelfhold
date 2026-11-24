"""The lifecycle of an installed plugin (M6 L09a, EF-SRC-020, D15).

`plugin_install` keeps the lifecycle of the third-party code it records: `pinned` freezes the
installed version (no update, no rollback), and the previous kept version — the one, and the only
one, the reserved rollback returns to — is the pair (`previous_version`, `previous_sha256`),
which comes and goes together (`previous_paired`). Nothing existing is touched: the columns
start NULL (or false) for every installed plugin, which is the truth (none of them was updated
yet). `source_binding` gains one `disabled_reason` value: `orphaned`, the mark of a binding
whose plugin was uninstalled — the binding stays, with everything it holds, and a reinstall of
the same key at the same signer reattaches it.

Both tables are rebuilt by SQLite batch mode, safe because migrations run with
`foreign_keys=OFF` followed by `foreign_key_check` (L02); the partial unique indexes of
`source_binding` are carried over by reflection. Reflection hands `CURRENT_TIMESTAMP` back in a
form SQLite re-renders with parentheses: the column is restated so the rebuilt tables keep the
DDL `create_all` writes (0005, 0111). Reversible: the downgrade maps an `orphaned` binding to
`not_loaded` (the plugin is gone: the source is not loaded) and drops the lifecycle columns —
the pin of a version nobody keeps is then simply forgotten.

Number: 0112 follows 0111 (the head of L08).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

_INSTALL = "plugin_install"
_BINDING = "source_binding"
_INSTALL_COLUMNS = ("pinned", "previous_version", "previous_sha256")
_PREVIOUS_PAIRED = "ck_plugin_install_previous_paired"
_PREVIOUS_PAIRED_SQL = (
    '("previous_version" IS NULL AND "previous_sha256" IS NULL)'
    ' OR ("previous_version" IS NOT NULL AND "previous_sha256" IS NOT NULL)'
)
_PREVIOUS_HASH = "ck_plugin_install_previous_sha256_sha256_prefixed"
_PREVIOUS_HASH_SQL = (
    'length("previous_sha256") = 71 AND "previous_sha256" GLOB \'sha256:*\' '
    "AND substr(\"previous_sha256\", 8) NOT GLOB '*[^0-9a-f]*'"
)
_REASON_CHECK = "ck_source_binding_disabled_reason_values"
# the values of `disabled_reason`, in the order the schema's tuple renders them
_REASONS_WITH_ORPHANED = (
    "'not_loaded', 'content_failures', 'removed_upstream', 'manual', 'orphaned'"
)
_REASONS_WITHOUT_ORPHANED = "'not_loaded', 'content_failures', 'removed_upstream', 'manual'"


def _columns(table: str) -> set[str]:
    return {row[1] for row in op.get_bind().execute(sa.text(f"PRAGMA table_info({table})"))}


def _has_check(table: str, name: str) -> bool:
    sql = (
        op.get_bind()
        .execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": table},
        )
        .scalar()
    )
    return sql is not None and f"CONSTRAINT {name}" in sql


def _batch(table: str) -> Any:
    """Reflection hands `CURRENT_TIMESTAMP` back in a form SQLite re-renders with parentheses:
    the column is restated so the rebuilt table keeps the DDL `create_all` writes."""
    created_at = sa.Column(
        "created_at" if table == _BINDING else "installed_at",
        sa.DateTime(),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    return op.batch_alter_table(table, recreate="always", reflect_args=(created_at,))


def upgrade() -> None:
    with _batch(_INSTALL) as batch:
        present = _columns(_INSTALL)
        if "pinned" not in present:  # a database created since the schema gained them has them
            batch.add_column(
                sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false())
            )
        for column in ("previous_version", "previous_sha256"):
            if column not in present:
                batch.add_column(sa.Column(column, sa.Text(), nullable=True))
        if not _has_check(_INSTALL, _PREVIOUS_PAIRED):
            batch.create_check_constraint(op.f(_PREVIOUS_PAIRED), _PREVIOUS_PAIRED_SQL)
        if not _has_check(_INSTALL, _PREVIOUS_HASH):
            batch.create_check_constraint(op.f(_PREVIOUS_HASH), _PREVIOUS_HASH_SQL)
    with _batch(_BINDING) as batch:
        batch.drop_constraint(op.f(_REASON_CHECK), type_="check")
        batch.create_check_constraint(
            op.f(_REASON_CHECK), f'"disabled_reason" IN ({_REASONS_WITH_ORPHANED})'
        )


def downgrade() -> None:
    # a binding whose plugin went away keeps its mark: the source is not loaded (the closest
    # of the reasons the older schema knows), never deleted — and this must happen while the
    # check still admits 'orphaned'
    op.get_bind().execute(
        sa.text(
            f"UPDATE {_BINDING} SET disabled_reason = 'not_loaded' "  # noqa: S608 - fixed table
            "WHERE disabled_reason = 'orphaned'"
        )
    )
    with _batch(_BINDING) as batch:
        batch.drop_constraint(op.f(_REASON_CHECK), type_="check")
        batch.create_check_constraint(
            op.f(_REASON_CHECK), f'"disabled_reason" IN ({_REASONS_WITHOUT_ORPHANED})'
        )
    with _batch(_INSTALL) as batch:
        if _has_check(_INSTALL, _PREVIOUS_HASH):
            batch.drop_constraint(op.f(_PREVIOUS_HASH), type_="check")
        if _has_check(_INSTALL, _PREVIOUS_PAIRED):
            batch.drop_constraint(op.f(_PREVIOUS_PAIRED), type_="check")
        present = _columns(_INSTALL)
        for column in _INSTALL_COLUMNS:
            if column in present:
                batch.drop_column(column)
