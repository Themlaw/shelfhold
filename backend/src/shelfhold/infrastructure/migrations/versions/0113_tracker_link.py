"""The tracker link of one profile (M6 L13, EF-INT-010): `tracker_link`, `tracker_allowance`.

One row per profile and per tracker plugin: the `TrackerRef` the plugin returned, the profile's
explicit accord to the push (never on by default), and the remote account's token sealed to the
row (ENF-SEC-005) with the fingerprint the API shows. `origin` is the api origin the token was
sealed for: the token follows the origin it was linked for, never the plugin key alone.
`tracker_allowance` is the admin's word
that one tracker may be linked by one child profile: a row means allowed, no row means refused.
Nothing existing is touched; reversible.

Number: 0113 is the number the plan reserved for L13. `down_revision` is 0114, the head of
main when the lot began; the numbers below 0113 are held by the other lots of the milestone,
whichever of them merges first re-links the chain.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0113"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE tracker_link (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	plugin_key VARCHAR NOT NULL,
	remote_ref TEXT NOT NULL,
	origin VARCHAR NOT NULL,
	linked_at DATETIME NOT NULL,
	push_enabled BOOLEAN DEFAULT 0 NOT NULL,
	secret_kid VARCHAR,
	secret_v INTEGER,
	secret_nonce BLOB,
	secret_ct BLOB,
	token_fingerprint VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_tracker_link PRIMARY KEY (id),
	CONSTRAINT ck_tracker_link_secret_sealed CHECK (("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL AND "secret_ct" IS NULL) OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL AND length("secret_ct") >= 16)),
	CONSTRAINT ck_tracker_link_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_tracker_link_remote_ref_json CHECK (json_valid("remote_ref")),
	CONSTRAINT ck_tracker_link_plugin_key_not_empty CHECK ("plugin_key" <> ''),
	CONSTRAINT ck_tracker_link_origin_not_empty CHECK ("origin" <> ''),
	CONSTRAINT ck_tracker_link_sealed CHECK ("secret_ct" IS NOT NULL),
	CONSTRAINT ck_tracker_link_fingerprint_iff_sealed CHECK (("token_fingerprint" IS NULL) = ("secret_ct" IS NULL)),
	CONSTRAINT uq_tracker_link_user_id_plugin_key UNIQUE (user_id, plugin_key),
	CONSTRAINT fk_tracker_link_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_tracker_link_secret_kid_secret_key FOREIGN KEY(secret_kid) REFERENCES secret_key (kid)
)
"""
    )
    op.execute(
        """
CREATE TABLE tracker_allowance (
	user_id VARCHAR(26) NOT NULL,
	plugin_key VARCHAR NOT NULL,
	allowed_by VARCHAR(26),
	allowed_at DATETIME NOT NULL,
	CONSTRAINT pk_tracker_allowance PRIMARY KEY (user_id, plugin_key),
	CONSTRAINT ck_tracker_allowance_plugin_key_not_empty CHECK ("plugin_key" <> ''),
	CONSTRAINT fk_tracker_allowance_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_tracker_allowance_allowed_by_user FOREIGN KEY(allowed_by) REFERENCES user (id) ON DELETE SET NULL
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE tracker_allowance")
    op.execute("DROP TABLE tracker_link")
