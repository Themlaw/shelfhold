"""A Mihon backup imported by an admin (M4 L26, EF-INT-008).

- `tachibk_import`: one batch per imported backup, for one profile: previewed, then applied once;
  it keeps what would be written (`staged`) until it is applied, and the hash of the file;
- `read_marker`: what the profile read in a series of the backup (a chapter proven read, or a page),
  waiting for the series to be resolved: one row per profile, series and chapter;
- `tracker_hint`: the tracker entry a series was linked to (a closed list of trackers, an
  identifier, how far the reader was): no address, no title, no token.

The series themselves are `pending_source_item` rows, which already exist. Nothing existing is
touched; reversible.

Number: 0095 follows 0094 (L39), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE tachibk_import (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	created_by VARCHAR(26),
	schema_version VARCHAR NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	state VARCHAR DEFAULT 'previewed' NOT NULL,
	staged TEXT,
	summary JSON DEFAULT '{}' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	applied_at DATETIME,
	CONSTRAINT pk_tachibk_import PRIMARY KEY (id),
	CONSTRAINT ck_tachibk_import_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_tachibk_import_state_values CHECK ("state" IN ('previewed', 'applied')),
	CONSTRAINT ck_tachibk_import_summary_object CHECK (json_valid("summary") AND json_type("summary") = 'object'),
	CONSTRAINT ck_tachibk_import_content_hash_length CHECK (length("content_hash") = 64),
	CONSTRAINT fk_tachibk_import_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_tachibk_import_created_by_user FOREIGN KEY(created_by) REFERENCES user (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_tachibk_import_user ON tachibk_import (user_id)")
    op.execute(
        """
CREATE TABLE read_marker (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	pending_item_id VARCHAR(26) NOT NULL,
	import_id VARCHAR(26),
	chapter_ref VARCHAR NOT NULL,
	number VARCHAR,
	read BOOLEAN DEFAULT 0 NOT NULL,
	last_page INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_read_marker PRIMARY KEY (id),
	CONSTRAINT ck_read_marker_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_read_marker_read_bool CHECK ("read" IN (0, 1)),
	CONSTRAINT ck_read_marker_chapter_ref_length CHECK (length("chapter_ref") BETWEEN 1 AND 2048),
	CONSTRAINT ck_read_marker_last_page_min CHECK ("last_page" IS NULL OR "last_page" >= 0),
	CONSTRAINT uq_read_marker_user_id_pending_item_id_chapter_ref UNIQUE (user_id, pending_item_id, chapter_ref),
	CONSTRAINT fk_read_marker_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_read_marker_pending_item_id_pending_source_item FOREIGN KEY(pending_item_id) REFERENCES pending_source_item (id) ON DELETE CASCADE,
	CONSTRAINT fk_read_marker_import_id_tachibk_import FOREIGN KEY(import_id) REFERENCES tachibk_import (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_read_marker_item ON read_marker (pending_item_id)")
    op.execute(
        """
CREATE TABLE tracker_hint (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	pending_item_id VARCHAR(26) NOT NULL,
	import_id VARCHAR(26),
	tracker VARCHAR NOT NULL,
	remote_id VARCHAR NOT NULL,
	last_chapter FLOAT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_tracker_hint PRIMARY KEY (id),
	CONSTRAINT ck_tracker_hint_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_tracker_hint_tracker_values CHECK ("tracker" IN ('myanimelist', 'anilist', 'kitsu', 'mangaupdates', 'shikimori', 'bangumi')),
	CONSTRAINT ck_tracker_hint_remote_id_length CHECK (length("remote_id") BETWEEN 1 AND 64),
	CONSTRAINT uq_tracker_hint_user_id_pending_item_id_tracker UNIQUE (user_id, pending_item_id, tracker),
	CONSTRAINT fk_tracker_hint_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_tracker_hint_pending_item_id_pending_source_item FOREIGN KEY(pending_item_id) REFERENCES pending_source_item (id) ON DELETE CASCADE,
	CONSTRAINT fk_tracker_hint_import_id_tachibk_import FOREIGN KEY(import_id) REFERENCES tachibk_import (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_tracker_hint_item ON tracker_hint (pending_item_id)")


def downgrade() -> None:
    op.drop_table("tracker_hint")
    op.drop_table("read_marker")
    op.drop_table("tachibk_import")
