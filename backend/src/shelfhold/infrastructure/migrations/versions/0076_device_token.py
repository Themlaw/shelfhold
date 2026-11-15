"""The device token (M4 L46, ADR-0053 §4, ADR-0049): the credential of an e-reader.

One table, `device_token`: a public identifier (`x-auth-user`, the user name of HTTP Basic), a
reader profile it acts as (`subject_user_id`, deleted with it), the account that issued it
(`issued_by`, for the audit only), one stored digest (SHA-256 of the md5 KOReader sends; never the
secret) and its life (expiry, last use, revocation).

The columns M3 laid on `api_key` do not suffice, and a token is not put there on purpose: an API
key is an `shk_…` bearer secret with scopes that the whole API accepts, a device token has a closed
reach (OPDS and kosync). Two tables, so that neither can be taken for the other. Every old row of
every table stays.
"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_token",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("public_id", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("subject_user_id", sa.String(length=26), nullable=False),
        sa.Column("issued_by", sa.String(length=26), nullable=True),
        sa.Column("secret_digest", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_device_token_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("secret_digest") = 71 AND "secret_digest" GLOB \'sha256:*\' '
            "AND substr(\"secret_digest\", 8) NOT GLOB '*[^0-9a-f]*'",
            name=op.f("ck_device_token_secret_digest_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("public_id") = 8', name=op.f("ck_device_token_public_id_length")
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"],
            ["user.id"],
            name=op.f("fk_device_token_issued_by_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["user.id"],
            name=op.f("fk_device_token_subject_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_token")),
        sa.UniqueConstraint("public_id", name=op.f("uq_device_token_public_id")),
    )
    op.create_index("ix_device_token_subject", "device_token", ["subject_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_device_token_subject", table_name="device_token")
    op.drop_table("device_token")
