"""`export_op.op = 'register'` (PLAN-M1a L28, bug found in L27 for `pull` destinations).

A `pull` destination only gets a manifest line, planned as a `register` operation, which the closed
list refused: such a plan could not even be stored. Taken with a migration token granted for L28.
`export_op` is rebuilt by SQLite batch mode (migrations run with `foreign_keys=OFF` then
`foreign_key_check`); `test_migrated_ddl_equals_create_all` keeps the revision and the metadata
honest.
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

CHECK = "ck_export_op_op_values"
BEFORE = ("mkdir", "write", "rename", "recycle", "delete", "touch_mtime", "api_call")
AFTER = (*BEFORE, "register")


def _listing(values: tuple[str, ...]) -> str:
    return '"op" IN (' + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("export_op", recreate="always") as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM export_op WHERE op = 'register'")
    with op.batch_alter_table("export_op", recreate="always") as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(BEFORE))
