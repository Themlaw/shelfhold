"""`bundle_member.page_map_state` learns `verified`, and `kavita_pages` (M4 L50, ADR-0053 §2).

A member of a block is `verified` when Kavita announces, for the chapter, the very number of pages
Shelfhold counted (the relay to Kavita goes through such members only). The state is one more value
of a CHECK, which SQLite cannot widen in place: the table is rebuilt, rows, index and the trigger of
`export_item` (which names it) written again. `kavita_pages` is what Kavita announced at the last
comparison (empty until one was made). Reversible: downgrading puts every `verified` member back to
`candidate` and drops the column.

Number: 0082 is the head of main when this lot was rebased (L49b).
"""

from alembic import op

# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None

_COLUMNS = (
    "version_id, rank, export_item_id, unit_id, chapter_label, content_fp, pages_digest, "
    "first_page, page_count, page_map_state"
)


def _create(table: str, *, states: str, extra_column: str, extra_check: str) -> None:
    op.execute(
        f"""
CREATE TABLE {table} (
	version_id VARCHAR(26) NOT NULL,
	rank INTEGER NOT NULL,
	export_item_id VARCHAR(26),
	unit_id VARCHAR(26),
	chapter_label VARCHAR NOT NULL,
	content_fp VARCHAR,
	pages_digest VARCHAR,
	first_page INTEGER NOT NULL,
	page_count INTEGER NOT NULL,
	page_map_state VARCHAR DEFAULT 'candidate' NOT NULL,{extra_column}
	CONSTRAINT pk_bundle_member PRIMARY KEY (version_id, rank),
	CONSTRAINT ck_bundle_member_page_map_state_values CHECK ("page_map_state" IN ({states})),{extra_check}
	CONSTRAINT ck_bundle_member_rank_min CHECK ("rank" >= 1),
	CONSTRAINT ck_bundle_member_pages_positive CHECK ("first_page" >= 1 AND "page_count" >= 0),
	CONSTRAINT ck_bundle_member_content_fp_hex64 CHECK ("content_fp" IS NULL OR (length("content_fp") = 64 AND "content_fp" NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT ck_bundle_member_pages_digest_hex64 CHECK ("pages_digest" IS NULL OR (length("pages_digest") = 64 AND "pages_digest" NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT fk_bundle_member_version_id_bundle_version FOREIGN KEY(version_id) REFERENCES bundle_version (id) ON DELETE CASCADE,
	CONSTRAINT fk_bundle_member_export_item_id_export_item FOREIGN KEY(export_item_id) REFERENCES export_item (id) ON DELETE SET NULL,
	CONSTRAINT fk_bundle_member_unit_id_unit FOREIGN KEY(unit_id) REFERENCES unit (id) ON DELETE SET NULL
)
"""
    )


_TRIGGER = (
    "CREATE TRIGGER export_item_wipe_bundle_members BEFORE DELETE ON export_item BEGIN "
    "UPDATE bundle_member SET content_fp = NULL, pages_digest = NULL "
    "WHERE export_item_id = OLD.id; END"
)


_COPY = f"INSERT INTO bundle_member_new ({_COLUMNS}) SELECT {_COLUMNS} FROM bundle_member"  # noqa: S608


def _swap() -> None:
    op.execute(_COPY)
    op.execute("DROP TRIGGER export_item_wipe_bundle_members")
    op.execute("DROP TABLE bundle_member")
    op.execute("ALTER TABLE bundle_member_new RENAME TO bundle_member")
    op.execute("CREATE INDEX ix_bundle_member_item ON bundle_member (export_item_id)")
    op.execute(_TRIGGER)


def upgrade() -> None:
    _create(
        "bundle_member_new",
        states="'candidate', 'unverified', 'verified'",
        extra_column="\n\tkavita_pages INTEGER,",
        extra_check='\n\tCONSTRAINT ck_bundle_member_kavita_pages_min CHECK ("kavita_pages" >= 0),',
    )
    _swap()


def downgrade() -> None:
    _create(
        "bundle_member_new", states="'candidate', 'unverified'", extra_column="", extra_check=""
    )
    op.execute(
        "UPDATE bundle_member SET page_map_state = 'candidate' WHERE page_map_state = 'verified'"
    )
    _swap()
