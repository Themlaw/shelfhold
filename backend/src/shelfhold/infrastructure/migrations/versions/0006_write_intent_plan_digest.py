"""`fs_intent.op = 'write'` and a hex `export_plan.base_version` (PLAN-M1a L27, review of PR #46).

A file written to a destination is now journaled like the other operations (02 section 5): the
`write` op joins the closed list. The version of a plan becomes the 128-bit digest (hex text) of
what it was computed from, instead of a 48-bit integer; existing integers stay valid (digits are
hex). Taken with the migration token granted for L27. `fs_intent` and `export_plan` are rebuilt by
SQLite batch mode (safe: migrations run with `foreign_keys=OFF` then `foreign_key_check`);
`test_migrated_ddl_equals_create_all` keeps the revision and the metadata honest.
"""

from contextlib import AbstractContextManager
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

CHECK = "ck_fs_intent_op_values"
BEFORE = (
    "rename", "upgrade", "recycle", "delete", "purge_upstream", "safeguard", "rmdir",
    "copy_cross_root", "unlink", "tombstone_rekey",
)  # fmt: skip
AFTER = (*BEFORE, "write")
VERSION_CHECK = "ck_export_plan_base_version_min"
VERSION_HEX = "ck_export_plan_base_version_hex"
HEX_RULE = "length(base_version) BETWEEN 1 AND 64 AND base_version NOT GLOB '*[^0-9a-f]*'"
INT_RULE = "typeof(base_version) = 'integer' AND base_version >= 0"


def _listing(values: tuple[str, ...]) -> str:
    return '"op" IN (' + ", ".join(f"'{v}'" for v in values) + ")"


def _rebuild(table: str = "fs_intent") -> AbstractContextManager[BatchOperations]:
    created_at: sa.Column[datetime] = sa.Column(
        "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    return op.batch_alter_table(table, recreate="always", reflect_args=(created_at,))


def upgrade() -> None:
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(AFTER))
    with _rebuild("export_plan") as batch:
        batch.drop_constraint(op.f(VERSION_CHECK), type_="check")
        batch.alter_column("base_version", type_=sa.String(), existing_nullable=False)
        batch.create_check_constraint(op.f(VERSION_HEX), HEX_RULE)


def downgrade() -> None:
    op.execute(  # a hex digest has no integer form
        "DELETE FROM export_plan "
        "WHERE base_version = '' OR base_version GLOB '*[^0-9]*' OR length(base_version) > 18"
    )
    with _rebuild("export_plan") as batch:
        batch.drop_constraint(op.f(VERSION_HEX), type_="check")
        batch.alter_column(
            "base_version",
            type_=sa.Integer(),
            existing_nullable=False,
            postgresql_using=None,
        )
        batch.create_check_constraint(op.f(VERSION_CHECK), INT_RULE)
    op.execute("DELETE FROM fs_intent WHERE op = 'write'")
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(BEFORE))
