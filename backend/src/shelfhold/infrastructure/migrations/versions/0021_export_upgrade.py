"""Same-path upgrade (PLAN-M2 L16, EF-EXP-016): the `export_upgrade` table.

One row per replacement of an exported file at its path: the hashes served before and after, the
bin entry of the old version and whether KOReader's position was reset. A new table only: no
existing row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0025"
branch_labels = None
depends_on = None


def _ulid(column: str) -> str:
    return f'length("{column}") = 26 AND "{column}" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\''


def _sha256(column: str) -> str:
    c = f'"{column}"'
    return f"length({c}) = 71 AND {c} GLOB 'sha256:*' AND substr({c}, 8) NOT GLOB '*[^0-9a-f]*'"


def upgrade() -> None:
    op.create_table(
        "export_upgrade",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("intent_id", sa.String(length=26), nullable=False),
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("export_item_id", sa.String(length=26), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("automatic", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("koreader_reset", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("old_content_id", sa.String(length=26), nullable=True),
        sa.Column("old_content_hash", sa.String(), nullable=True),
        sa.Column("old_sidecar_sha256", sa.String(), nullable=True),
        sa.Column("old_file_hash", sa.String(), nullable=False),
        sa.Column("new_file_hash", sa.String(), nullable=False),
        sa.Column("trash_item_id", sa.String(length=26), nullable=True),
        sa.Column("state", sa.String(), server_default="done", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.Column("restored_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(_ulid("id"), name=op.f("ck_export_upgrade_id_ulid")),
        sa.CheckConstraint(_ulid("intent_id"), name=op.f("ck_export_upgrade_intent_id_ulid")),
        sa.CheckConstraint(
            "\"reason\" IN ('better_version', 'preferred_team', 'integrity_reacquire', 'restore')",
            name=op.f("ck_export_upgrade_reason_values"),
        ),
        sa.CheckConstraint(
            "\"state\" IN ('done', 'restored')", name=op.f("ck_export_upgrade_state_values")
        ),
        sa.CheckConstraint(
            _sha256("old_content_hash"),
            name=op.f("ck_export_upgrade_old_content_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            _sha256("old_sidecar_sha256"),
            name=op.f("ck_export_upgrade_old_sidecar_sha256_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            _sha256("old_file_hash"),
            name=op.f("ck_export_upgrade_old_file_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            _sha256("new_file_hash"),
            name=op.f("ck_export_upgrade_new_file_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            '("state" = \'restored\') = ("restored_at" IS NOT NULL)',
            name=op.f("ck_export_upgrade_restored_at_iff_restored"),
        ),
        sa.ForeignKeyConstraint(
            ["shelf_destination_id"],
            ["shelf_destination.id"],
            name=op.f("fk_export_upgrade_shelf_destination_id_shelf_destination"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["export_item_id"],
            ["export_item.id"],
            name=op.f("fk_export_upgrade_export_item_id_export_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["unit.id"],
            name=op.f("fk_export_upgrade_unit_id_unit"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["old_content_id"],
            ["content.id"],
            name=op.f("fk_export_upgrade_old_content_id_content"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trash_item_id"],
            ["trash_item.id"],
            name=op.f("fk_export_upgrade_trash_item_id_trash_item"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_upgrade")),
    )
    op.create_index("ix_export_upgrade_item", "export_upgrade", ["export_item_id"])
    op.create_index("ix_export_upgrade_created_at", "export_upgrade", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_export_upgrade_created_at", table_name="export_upgrade")
    op.drop_index("ix_export_upgrade_item", table_name="export_upgrade")
    op.drop_table("export_upgrade")
