"""`trash_item.deletion_id` and `trash_item.snapshot`: what a restore needs (M3 L25, EF-BIB-021).

A deletion of content (levels "remove from destinations" and "delete the content") moves files
into the bin of their root and forgets their rows. To put a file back exactly as it was, the bin
entry keeps what was forgotten: `snapshot` (the registry row of the copy and the record of the
export that served it, as JSON) and `deletion_id` (the run it belongs to, so that one restore
puts back one deletion). Both NULL for the bin entries of before: those can be swept but not
restored. No row is touched.

A restore moves files back under intents of their own: `fs_intent.op = 'unbin'` joins the closed
list (the table is rebuilt by SQLite batch mode, as 0006 did; `test_migrated_ddl_equals_create_all`
keeps the revision and the metadata honest).
"""

from contextlib import AbstractContextManager
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

CHECK = "ck_fs_intent_op_values"
BEFORE = (
    "rename", "upgrade", "recycle", "delete", "purge_upstream", "safeguard", "rmdir",
    "copy_cross_root", "unlink", "tombstone_rekey", "write",
)  # fmt: skip
AFTER = (*BEFORE, "unbin")


def _listing(values: tuple[str, ...]) -> str:
    return '"op" IN (' + ", ".join(f"'{v}'" for v in values) + ")"


def _rebuild() -> AbstractContextManager[BatchOperations]:
    created_at: sa.Column[datetime] = sa.Column(
        "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    return op.batch_alter_table("fs_intent", recreate="always", reflect_args=(created_at,))


def upgrade() -> None:
    op.add_column("trash_item", sa.Column("deletion_id", sa.String(26), nullable=True))
    op.add_column("trash_item", sa.Column("snapshot", sa.JSON(none_as_null=True), nullable=True))
    op.create_index("ix_trash_item_deletion", "trash_item", ["deletion_id"])
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(AFTER))


def downgrade() -> None:
    open_unbin = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM fs_intent "
                "WHERE op = 'unbin' AND state IN ('pending', 'applying')"
            )
        )
        .scalar_one()
    )
    if open_unbin:  # a restore whose files are still in a bin: never erase what finishes it
        raise RuntimeError(
            f"cannot downgrade past 0055: {open_unbin} restore intent(s) (fs_intent.op = 'unbin') "
            "are still open. Start the instance so that it finishes them (or resolve them with "
            "`shelfhold intents`), then downgrade."
        )
    op.execute("DELETE FROM fs_intent WHERE op = 'unbin'")  # the closed ones only
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(BEFORE))
    op.drop_index("ix_trash_item_deletion", table_name="trash_item")
    op.drop_column("trash_item", "snapshot")
    op.drop_column("trash_item", "deletion_id")
