"""The chapters whose pages could not be normalized (M4 L53, ADR-0054).

One table, `page_issue`: for a Unit, the closed code of the first page that was refused and how many
times the decoder died on one, for the content it was found on (`original_sha256`). It goes with its
Unit (`ON DELETE CASCADE`). Nothing existing is touched; reversible.

Number: 0085 follows 0084 (L15), the head of main when this lot was rebased.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE page_issue (
	unit_id VARCHAR(26) NOT NULL,
	original_sha256 VARCHAR NOT NULL,
	code VARCHAR NOT NULL,
	failures INTEGER DEFAULT '0' NOT NULL,
	noted_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_page_issue PRIMARY KEY (unit_id),
	CONSTRAINT ck_page_issue_unit_id_ulid CHECK (length("unit_id") = 26 AND "unit_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_page_issue_original_sha256_sha256_prefixed CHECK (length("original_sha256") = 71 AND "original_sha256" GLOB 'sha256:*' AND substr("original_sha256", 8) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_page_issue_code_values CHECK ("code" IN ('page.empty', 'page.undecodable', 'page.too_large', 'page.format_unsupported', 'pages.worker_failed')),
	CONSTRAINT ck_page_issue_failures_min CHECK ("failures" >= 0),
	CONSTRAINT fk_page_issue_unit_id_unit FOREIGN KEY(unit_id) REFERENCES unit (id) ON DELETE CASCADE
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE page_issue")
