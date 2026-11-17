"""The Apprise destination of one adult profile, sealed (M4 L39, EF-INT-021, ENF-SEC-005).

`notify_destination`: one row per profile that has a destination of its own, the URL in the sealed
envelope columns of every secret (rotated with the others). Nothing existing is touched;
reversible.

Number: 0094 follows 0093 (L18b), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE notify_destination (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	secret_kid VARCHAR,
	secret_v INTEGER,
	secret_nonce BLOB,
	secret_ct BLOB,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_notify_destination PRIMARY KEY (id),
	CONSTRAINT ck_notify_destination_secret_sealed CHECK (("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL AND "secret_ct" IS NULL) OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL AND length("secret_ct") >= 16)),
	CONSTRAINT ck_notify_destination_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_notify_destination_holds_a_secret CHECK ("secret_ct" IS NOT NULL),
	CONSTRAINT uq_notify_destination_user_id UNIQUE (user_id),
	CONSTRAINT fk_notify_destination_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_notify_destination_secret_kid_secret_key FOREIGN KEY(secret_kid) REFERENCES secret_key (kid)
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE notify_destination")
