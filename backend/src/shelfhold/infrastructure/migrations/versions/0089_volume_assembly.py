"""A volume assembled from chapters: `export_assembly`, and `fs_intent.op = 'assemble'` (M4 L10).

`replace(old_items[], new_item)` (EF-EXP-019, EF-BIB-044): the chapter files of a destination are
replaced by one volume under one intent. The closed list of `fs_intent.op` gains `assemble` (the
table is rebuilt by SQLite batch mode, as 0081 did) and `export_assembly` records what each
assembly replaced. It goes with its destination and with the volume's export item
(`ON DELETE CASCADE`). The chapter files are in the bin of their root, never deleted. No row is
touched; reversible once no `assemble` intent is open (the assemblies recorded go with the table).

Number: 0089 follows 0088, the head of main when this lot was rebased.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from contextlib import AbstractContextManager
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

CHECK = "ck_fs_intent_op_values"
BEFORE = (
    "rename", "upgrade", "recycle", "delete", "purge_upstream", "safeguard", "rmdir",
    "copy_cross_root", "unlink", "tombstone_rekey", "write", "unbin", "utime",
)  # fmt: skip
AFTER = (*BEFORE, "assemble")


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
    op.execute(
        """
CREATE TABLE export_assembly (
	id VARCHAR(26) NOT NULL,
	intent_id VARCHAR(26) NOT NULL,
	shelf_destination_id VARCHAR(26) NOT NULL,
	export_item_id VARCHAR(26) NOT NULL,
	volume VARCHAR NOT NULL,
	old_items JSON NOT NULL,
	derived_hash VARCHAR NOT NULL,
	file_hash VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_export_assembly PRIMARY KEY (id),
	CONSTRAINT ck_export_assembly_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_export_assembly_intent_id_ulid CHECK (length("intent_id") = 26 AND "intent_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_export_assembly_old_items_list CHECK (json_valid("old_items") AND json_type("old_items") = 'array'),
	CONSTRAINT ck_export_assembly_derived_hash_sha256_prefixed CHECK (length("derived_hash") = 71 AND "derived_hash" GLOB 'sha256:*' AND substr("derived_hash", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_export_assembly_file_hash_sha256_prefixed CHECK (length("file_hash") = 71 AND "file_hash" GLOB 'sha256:*' AND substr("file_hash", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT uq_export_assembly_intent UNIQUE (intent_id),
	CONSTRAINT uq_export_assembly_item UNIQUE (export_item_id),
	CONSTRAINT fk_export_assembly_shelf_destination_id_shelf_destination FOREIGN KEY(shelf_destination_id) REFERENCES shelf_destination (id) ON DELETE CASCADE,
	CONSTRAINT fk_export_assembly_export_item_id_export_item FOREIGN KEY(export_item_id) REFERENCES export_item (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        "CREATE INDEX ix_export_assembly_destination ON export_assembly (shelf_destination_id)"
    )


def downgrade() -> None:
    open_assemble = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM fs_intent "
                "WHERE op = 'assemble' AND state IN ('pending', 'applying')"
            )
        )
        .scalar_one()
    )
    if open_assemble:  # a volume still to be settled: never erase what finishes it
        raise RuntimeError(
            f"cannot downgrade past 0089: {open_assemble} assemble intent(s) are still open. "
            "Start the instance so that it finishes them (or resolve them with `shelfhold "
            "intents`), then downgrade."
        )
    op.execute("DROP TABLE export_assembly")
    op.execute("DELETE FROM fs_intent WHERE op = 'assemble'")  # the closed ones only
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CHECK), type_="check")
        batch.create_check_constraint(op.f(CHECK), _listing(BEFORE))
