"""KEPUB renditions (M5 L20b, EF-BIB-015): `rendition.kind` takes `kepub`.

The cache of L20a holds converted images (`jpeg`, `png`); a book made from an EPUB for a Kobo is a
third kind. SQLite cannot widen a `CHECK`, so the table is rebuilt with the wider closed list and
its rows are copied: key, hash checks, JSON and size checks and index are the ones of 0104.
Reversible: the way back keeps the image rows and drops the KEPUB ones (a regenerable cache, its
files are swept by the orphan collection).

Number: 0108 is the one the launch of L20b names (0105 to 0107 are reserved to other lots).
`down_revision` is the head of main (0104, L20a).
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0108"
down_revision = "0104"
branch_labels = None
depends_on = None


def _create(kinds: str) -> str:
    return f"""
CREATE TABLE rendition (
	source_content_hash VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	params JSON DEFAULT '{{}}' NOT NULL,
	content_hash VARCHAR NOT NULL,
	size BIGINT NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_rendition PRIMARY KEY (source_content_hash, kind, params),
	CONSTRAINT ck_rendition_source_content_hash_sha256_prefixed CHECK (length("source_content_hash") = 71 AND "source_content_hash" GLOB 'sha256:*' AND substr("source_content_hash", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_rendition_content_hash_sha256_prefixed CHECK (length("content_hash") = 71 AND "content_hash" GLOB 'sha256:*' AND substr("content_hash", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_rendition_kind_values CHECK ("kind" IN ({kinds})),
	CONSTRAINT ck_rendition_params_object CHECK (json_valid("params") AND json_type("params") = 'object'),
	CONSTRAINT ck_rendition_size_min CHECK ("size" >= 1)
)
"""


def _rebuild(kinds: str, keep: str) -> None:
    op.execute("ALTER TABLE rendition RENAME TO rendition_old")
    op.execute("DROP INDEX ix_rendition_content_hash")
    op.execute(_create(kinds))
    copy = (
        "INSERT INTO rendition (source_content_hash, kind, params, content_hash, size, created_at) "
        "SELECT source_content_hash, kind, params, content_hash, size, created_at "
        "FROM rendition_old WHERE " + keep  # a constant of this file, no input
    )
    op.execute(copy)
    op.execute("DROP TABLE rendition_old")
    op.execute("CREATE INDEX ix_rendition_content_hash ON rendition (content_hash)")


def upgrade() -> None:
    _rebuild("'jpeg', 'png', 'kepub'", "1")


def downgrade() -> None:
    _rebuild("'jpeg', 'png'", "kind IN ('jpeg', 'png')")
