"""`fs_intent.op = 'utime'`: the change of a file's modification time, under an intent (M4 L04).

A destination plan can ask `touch_mtime`. The host no longer rewrites the file for it (the inode,
its hard links and its owner stay): it sets the time in place, and the operation is an intent of
its own, which the boot replays forward (the same file, the same hash, the time put again) or
closes. The closed list of `fs_intent.op` gains `utime` (the table is rebuilt by SQLite batch mode,
as 0055 did; `test_migrated_ddl_equals_create_all` keeps the revision and the metadata honest).
No row is touched; reversible once no `utime` intent is open.

Number: 0080 is that of L17, the head of main when this lot was rebased.
"""

from contextlib import AbstractContextManager
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None

CHECK = "ck_fs_intent_op_values"
BEFORE = (
    "rename", "upgrade", "recycle", "delete", "purge_upstream", "safeguard", "rmdir",
    "copy_cross_root", "unlink", "tombstone_rekey", "write", "unbin",
)  # fmt: skip
AFTER = (*BEFORE, "utime")


def _listing(values: tuple[str, ...]) -> str:
    return '"op" IN (' + ", ".join(f"'{v}'" for v in values) + ")"


def _rebuild() -> AbstractContextManager[BatchOperations]:
    created_at: sa.Column[datetime] = sa.Column(
        "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    return op.batch_alter_table("fs_intent", recreate="always", reflect_args=(created_at,))


def upgrade() -> None:
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(AFTER))


def downgrade() -> None:
    open_utime = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM fs_intent "
                "WHERE op = 'utime' AND state IN ('pending', 'applying')"
            )
        )
        .scalar_one()
    )
    if open_utime:  # a time still to be set: never erase what finishes it
        raise RuntimeError(
            f"cannot downgrade past 0081: {open_utime} utime intent(s) are still open. Start the "
            "instance so that it finishes them (or resolve them with `shelfhold intents`), then "
            "downgrade."
        )
    op.execute("DELETE FROM fs_intent WHERE op = 'utime'")  # the closed ones only
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(BEFORE))
