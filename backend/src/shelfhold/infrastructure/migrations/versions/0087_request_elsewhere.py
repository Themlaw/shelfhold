"""A request put aside to be bought or borrowed elsewhere, and what the source search proposed for
a request nobody could resolve (M4 L30, EF-USR-019, D22).

`request.state` takes two values more (`to_buy`, `borrow_elsewhere`) and `request.elsewhere_url`
keeps the official or library address the admin gave (`https` only). SQLite cannot change a
`CHECK`: the table is rebuilt (renamed, made again, copied, the old one dropped), with the same
columns in the same order. `request_proposal` keeps up to five results of the search per request,
which go with the request and with the result. Nothing refers to `request` by a foreign key.

Reversible: a request put aside goes back to `pending` (the address and the proposals are lost).

Number: 0087 follows 0086 (L18a), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

_COLUMNS = "id, requester_user_id, target_kind, target_value, state, reject_reason, created_at, updated_at, visibility, work_id, edition_id, shelf_id, decided_by, decided_at, approved_age, approved_x, arrived_at, started_at, shown_title"

_REQUEST_NEW = """
CREATE TABLE request (
	id VARCHAR(26) NOT NULL,
	requester_user_id VARCHAR(26) NOT NULL,
	target_kind VARCHAR NOT NULL,
	target_value VARCHAR NOT NULL,
	state VARCHAR DEFAULT 'pending' NOT NULL,
	reject_reason VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, visibility VARCHAR DEFAULT 'private' NOT NULL, work_id VARCHAR(26), edition_id VARCHAR(26), shelf_id VARCHAR(26), decided_by VARCHAR, decided_at DATETIME, approved_age INTEGER, approved_x BOOLEAN DEFAULT 0 NOT NULL, arrived_at DATETIME, started_at DATETIME, shown_title VARCHAR, elsewhere_url VARCHAR,
	CONSTRAINT pk_request PRIMARY KEY (id),
	CONSTRAINT ck_request_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_request_target_kind_values CHECK ("target_kind" IN ('work_id', 'source_ref', 'isbn', 'free_text')),
	CONSTRAINT ck_request_state_values CHECK ("state" IN ('pending', 'approved', 'rejected', 'not_found', 'cancelled', 'arrived', 'to_buy', 'borrow_elsewhere')),
	CONSTRAINT ck_request_target_value_not_empty CHECK ("target_value" <> ''),
	CONSTRAINT ck_request_work_target_is_ulid CHECK ("target_kind" <> 'work_id' OR (length("target_value") = 26 AND "target_value" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*')),
	CONSTRAINT ck_request_reason_only_on_refusal CHECK ("reject_reason" IS NULL OR "state" = 'rejected'),
	CONSTRAINT ck_request_elsewhere_url_https CHECK ("elsewhere_url" IS NULL OR ("elsewhere_url" LIKE 'https://%' AND length("elsewhere_url") <= 2048)),
	CONSTRAINT fk_request_requester_user_id_user FOREIGN KEY(requester_user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""

_REQUEST_OLD = """
CREATE TABLE request (
	id VARCHAR(26) NOT NULL,
	requester_user_id VARCHAR(26) NOT NULL,
	target_kind VARCHAR NOT NULL,
	target_value VARCHAR NOT NULL,
	state VARCHAR DEFAULT 'pending' NOT NULL,
	reject_reason VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL, visibility VARCHAR DEFAULT 'private' NOT NULL, work_id VARCHAR(26), edition_id VARCHAR(26), shelf_id VARCHAR(26), decided_by VARCHAR, decided_at DATETIME, approved_age INTEGER, approved_x BOOLEAN DEFAULT 0 NOT NULL, arrived_at DATETIME, started_at DATETIME, shown_title VARCHAR,
	CONSTRAINT pk_request PRIMARY KEY (id),
	CONSTRAINT ck_request_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_request_target_kind_values CHECK ("target_kind" IN ('work_id', 'source_ref', 'isbn', 'free_text')),
	CONSTRAINT ck_request_state_values CHECK ("state" IN ('pending', 'approved', 'rejected', 'not_found', 'cancelled', 'arrived')),
	CONSTRAINT ck_request_target_value_not_empty CHECK ("target_value" <> ''),
	CONSTRAINT ck_request_work_target_is_ulid CHECK ("target_kind" <> 'work_id' OR (length("target_value") = 26 AND "target_value" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*')),
	CONSTRAINT ck_request_reason_only_on_refusal CHECK ("reject_reason" IS NULL OR "state" = 'rejected'),
	CONSTRAINT fk_request_requester_user_id_user FOREIGN KEY(requester_user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""


def _rebuild(ddl: str, select: str) -> None:
    op.execute("ALTER TABLE request RENAME TO request_old")
    op.execute(ddl)
    op.execute(f"INSERT INTO request ({_COLUMNS}) SELECT {select} FROM request_old")  # noqa: S608
    op.execute("DROP TABLE request_old")  # its two indexes go with it
    op.execute("CREATE INDEX ix_request_requester ON request (requester_user_id, state)")
    op.execute("CREATE INDEX ix_request_work ON request (work_id)")


def upgrade() -> None:
    _rebuild(_REQUEST_NEW, _COLUMNS)
    op.execute(
        """
CREATE TABLE request_proposal (
	id VARCHAR(26) NOT NULL,
	request_id VARCHAR(26) NOT NULL,
	source_item_id VARCHAR(26) NOT NULL,
	position INTEGER NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_request_proposal PRIMARY KEY (id),
	CONSTRAINT ck_request_proposal_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_request_proposal_position_range CHECK ("position" BETWEEN 1 AND 5),
	CONSTRAINT uq_request_proposal_request_id_source_item_id UNIQUE (request_id, source_item_id),
	CONSTRAINT uq_request_proposal_request_id_position UNIQUE (request_id, position),
	CONSTRAINT fk_request_proposal_request_id_request FOREIGN KEY(request_id) REFERENCES request (id) ON DELETE CASCADE,
	CONSTRAINT fk_request_proposal_source_item_id_pending_source_item FOREIGN KEY(source_item_id) REFERENCES pending_source_item (id) ON DELETE CASCADE
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE request_proposal")
    state = "CASE WHEN state IN ('to_buy', 'borrow_elsewhere') THEN 'pending' ELSE state END"
    _rebuild(_REQUEST_OLD, _COLUMNS.replace("state,", state + ",", 1))
