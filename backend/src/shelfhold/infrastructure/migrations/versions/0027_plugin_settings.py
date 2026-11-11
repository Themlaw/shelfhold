"""Per-source settings and attested trust values (PLAN-M2 L07, EF-SRC-013, EF-EXP-065).

`source_secret`: one sealed value per source and field (a secret field of `settings_schema`, or the
cookie jar). Its id `<plugin>/<kind>/<field>/<generation>` is the row coordinate of the envelope's
associated data, and the generation grows on every write: an older envelope put back does not
open. `plugin_attestation.plugin_version` and `plugin_sha256`: the installed version and files
digest an attestation was given for; the rows written before this revision get '' and match
nothing, so they lapse (the safe side). No other row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

_SEALED = (
    '("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL '
    'AND "secret_ct" IS NULL) '
    'OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 '
    'AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL '
    'AND length("secret_ct") >= 16)'
)


def upgrade() -> None:
    op.create_table(
        "source_secret",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("secret_kid", sa.String(), nullable=True),
        sa.Column("secret_v", sa.Integer(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ct", sa.LargeBinary(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.CheckConstraint(_SEALED, name=op.f("ck_source_secret_secret_sealed")),
        sa.CheckConstraint(
            "\"kind\" IN ('secret', 'cookies')", name=op.f("ck_source_secret_kind_values")
        ),
        sa.CheckConstraint(
            '"id" = "plugin_id" || \'/\' || "kind" || \'/\' || "field" || \'/\' || "generation"',
            name=op.f("ck_source_secret_id_coordinates"),
        ),
        sa.CheckConstraint('"secret_ct" IS NOT NULL', name=op.f("ck_source_secret_sealed")),
        sa.CheckConstraint('"generation" >= 1', name=op.f("ck_source_secret_generation_min")),
        sa.CheckConstraint(
            "\"plugin_id\" <> '' AND \"field\" <> ''", name=op.f("ck_source_secret_named")
        ),
        sa.ForeignKeyConstraint(
            ["secret_kid"], ["secret_key.kid"], name=op.f("fk_source_secret_secret_kid_secret_key")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_secret")),
        sa.UniqueConstraint(
            "plugin_id", "kind", "field", name=op.f("uq_source_secret_plugin_id_kind_field")
        ),
    )
    with op.batch_alter_table("plugin_attestation") as batch:
        batch.add_column(
            sa.Column("plugin_version", sa.String(), server_default=sa.text("''"), nullable=False)
        )
        batch.add_column(
            sa.Column("plugin_sha256", sa.String(), server_default=sa.text("''"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("plugin_attestation") as batch:
        batch.drop_column("plugin_sha256")
        batch.drop_column("plugin_version")
    op.drop_table("source_secret")
