"""The choices of one profile in the reader (M5 L13, EF-LEC-006): `reader_settings`.

A direction that may be pinned to a series (or the profile's own default) and the comfort of one
device (or of every one of them); the priority per type lives in the application. One row per
profile and place, and it goes with its profile. Nothing existing is touched; reversible.

Number: 0103 is the number the plan reserved for L13. `down_revision` is 0102, the head of this
branch's base; it is re-chained at the merge if another migration lands first, and the test that
pins the head follows. The file stays frozen in `tools/published-migrations.toml`; its
fingerprint is recomputed in the same commit as the re-chaining.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE reader_settings (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	scope VARCHAR NOT NULL,
	device_id VARCHAR,
	kind VARCHAR NOT NULL,
	"key" VARCHAR NOT NULL,
	value VARCHAR NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_reader_settings PRIMARY KEY (id),
	CONSTRAINT ck_reader_settings_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_reader_settings_scope_values CHECK ("scope" IN ('device', 'everywhere')),
	CONSTRAINT ck_reader_settings_kind_values CHECK ("kind" IN ('direction', 'comfort')),
	CONSTRAINT ck_reader_settings_device_named CHECK (("scope" = 'device') = ("device_id" IS NOT NULL)),
	CONSTRAINT ck_reader_settings_key_length CHECK (length("key") BETWEEN 1 AND 64),
	CONSTRAINT ck_reader_settings_value_length CHECK (length("value") BETWEEN 1 AND 64),
	CONSTRAINT fk_reader_settings_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_reader_settings_place ON reader_settings"
        ' (user_id, kind, "key", scope, ifnull(device_id, \'\'))'
    )


def downgrade() -> None:
    op.execute("DROP INDEX ux_reader_settings_place")
    op.execute("DROP TABLE reader_settings")
