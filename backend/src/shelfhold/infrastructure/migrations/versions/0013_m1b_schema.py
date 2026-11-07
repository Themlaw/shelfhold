"""M1b schema (PLAN-M1b L01): passkeys, backup runs, identity of the connected service.

Adds `webauthn_credential` (EF-USR-025), `backup_run` (EF-INT-015) and the instance fingerprint
of a connected service on `source_config` (EF-SRC-036). Nothing else: the consecutive content
error counter already lives in `source_failure` (0003, EF-BIB-042) and the upstream purge setting
in `shelf.purge_upstream_after_export` (0004, D16). Existing rows are untouched; the new columns
of `source_config` are nullable.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

ULID = 'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\''


def upgrade() -> None:
    op.create_table(
        "webauthn_credential",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("transports", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("aaguid", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(ULID, name=op.f("ck_webauthn_credential_id_ulid")),
        sa.CheckConstraint('"sign_count" >= 0', name=op.f("ck_webauthn_credential_sign_count_min")),
        sa.CheckConstraint(
            'json_valid("transports") AND json_type("transports") = \'array\'',
            name=op.f("ck_webauthn_credential_transports_list"),
        ),
        sa.CheckConstraint("\"name\" <> ''", name=op.f("ck_webauthn_credential_name_not_empty")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_webauthn_credential_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webauthn_credential")),
        sa.UniqueConstraint("credential_id", name=op.f("uq_webauthn_credential_credential_id")),
    )
    op.create_index("ix_webauthn_credential_user", "webauthn_credential", ["user_id"], unique=False)

    op.create_table(
        "backup_run",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(), server_default="running", nullable=False),
        sa.Column("cause", sa.String(), nullable=True),
        sa.Column("file_name", sa.String(), nullable=True),
        sa.Column("size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("schema_revision", sa.String(), nullable=True),
        sa.CheckConstraint(ULID, name=op.f("ck_backup_run_id_ulid")),
        sa.CheckConstraint(
            "\"state\" IN ('running', 'ok', 'failed')", name=op.f("ck_backup_run_state_values")
        ),
        sa.CheckConstraint('"size" >= 0', name=op.f("ck_backup_run_size_min")),
        sa.CheckConstraint(
            '"sha256" IS NULL OR (length("sha256") = 64 AND "sha256" NOT GLOB \'*[^0-9a-f]*\')',
            name=op.f("ck_backup_run_sha256_hex"),
        ),
        sa.CheckConstraint(
            '"state" <> \'running\' OR "finished_at" IS NULL',
            name=op.f("ck_backup_run_running_open"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_run")),
    )
    op.create_index("ix_backup_run_started", "backup_run", ["started_at"], unique=False)

    op.add_column("source_config", sa.Column("instance_fingerprint", sa.String(), nullable=True))
    op.add_column(
        "source_config", sa.Column("instance_fingerprint_seen_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("source_config", "instance_fingerprint_seen_at")
    op.drop_column("source_config", "instance_fingerprint")
    op.drop_index("ix_backup_run_started", table_name="backup_run")
    op.drop_table("backup_run")
    op.drop_index("ix_webauthn_credential_user", table_name="webauthn_credential")
    op.drop_table("webauthn_credential")
