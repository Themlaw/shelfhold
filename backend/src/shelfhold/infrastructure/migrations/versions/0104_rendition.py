"""Renditions (M5 L20a, EF-BIB-015): `rendition`, the ledger of converted images.

A client that lacks a format (AVIF, WebP) is served a JPEG or a PNG made from it once, by the
network-less worker, and kept under `/data/cache/renditions` by the hash of what was made. The row
is keyed by what was asked (the hash of the source content, the kind, the canonical parameters);
a purge erases the files, then the rows. A cache: regenerable, left out of the export. Nothing
existing is touched; reversible.

Number: 0104 is the one PLAN-M5 reserves for L20a. `down_revision` is the head of main (0102, L07).
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0104"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE rendition (
	source_content_hash VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	params JSON DEFAULT '{}' NOT NULL,
	content_hash VARCHAR NOT NULL,
	size BIGINT NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_rendition PRIMARY KEY (source_content_hash, kind, params),
	CONSTRAINT ck_rendition_source_content_hash_sha256_prefixed CHECK (length("source_content_hash") = 71 AND "source_content_hash" GLOB 'sha256:*' AND substr("source_content_hash", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_rendition_content_hash_sha256_prefixed CHECK (length("content_hash") = 71 AND "content_hash" GLOB 'sha256:*' AND substr("content_hash", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_rendition_kind_values CHECK ("kind" IN ('jpeg', 'png')),
	CONSTRAINT ck_rendition_params_object CHECK (json_valid("params") AND json_type("params") = 'object'),
	CONSTRAINT ck_rendition_size_min CHECK ("size" >= 1)
)
"""
    )
    op.execute("CREATE INDEX ix_rendition_content_hash ON rendition (content_hash)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_rendition_content_hash")
    op.execute("DROP TABLE rendition")
