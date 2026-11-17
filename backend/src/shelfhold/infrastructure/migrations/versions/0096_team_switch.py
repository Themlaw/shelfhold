"""The switch of an Edition to its preferred team (M4 L35, EF-BIB-005).

- `team_switch`: one row per preview of the switch, then its application: what the admin read (the
  Units that would switch, the blocked ones, the pending ones), the digest of it, and the report of
  what was done. A preview gone stale is never applied: the digest is compared first.

Nothing existing is touched; reversible.

Number: 0096 follows 0095 (L26), the head of main when this lot was written; renumbered at the
freeze if another lot took it first.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE team_switch (
	id VARCHAR(26) NOT NULL,
	edition_id VARCHAR(26) NOT NULL,
	team_id VARCHAR(26) NOT NULL,
	created_by VARCHAR(26),
	state VARCHAR DEFAULT 'previewed' NOT NULL,
	token VARCHAR(64) NOT NULL,
	preview JSON DEFAULT '{}' NOT NULL,
	report JSON,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	applied_at DATETIME,
	CONSTRAINT pk_team_switch PRIMARY KEY (id),
	CONSTRAINT ck_team_switch_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_team_switch_state_values CHECK ("state" IN ('previewed', 'queued', 'applying', 'done', 'stale', 'failed')),
	CONSTRAINT ck_team_switch_preview_object CHECK (json_valid("preview") AND json_type("preview") = 'object'),
	CONSTRAINT ck_team_switch_token_length CHECK (length("token") = 64),
	CONSTRAINT fk_team_switch_edition_id_edition FOREIGN KEY(edition_id) REFERENCES edition (id) ON DELETE CASCADE,
	CONSTRAINT fk_team_switch_team_id_team FOREIGN KEY(team_id) REFERENCES team (id) ON DELETE CASCADE,
	CONSTRAINT fk_team_switch_created_by_user FOREIGN KEY(created_by) REFERENCES user (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_team_switch_edition ON team_switch (edition_id)")


def downgrade() -> None:
    op.execute("DROP TABLE team_switch")
