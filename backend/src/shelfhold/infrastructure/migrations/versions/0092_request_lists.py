"""Imported reading lists (M4 L31, EF-USR-020).

- `request_list_import`: one batch per import, for one profile, with the reading shelf made for it;
- `request_list_item`: one row per line of the list, either already in the library (`in_library`)
  or a request (`requested`), so that the admin reads the whole list on one screen.

Nothing existing is touched; reversible.

Number: 0092 follows 0091 (L29), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE request_list_import (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	created_by VARCHAR(26),
	name VARCHAR NOT NULL,
	collection_id VARCHAR(26),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_request_list_import PRIMARY KEY (id),
	CONSTRAINT ck_request_list_import_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_request_list_import_name_length CHECK (length("name") BETWEEN 1 AND 200),
	CONSTRAINT fk_request_list_import_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE,
	CONSTRAINT fk_request_list_import_created_by_user FOREIGN KEY(created_by) REFERENCES user (id) ON DELETE SET NULL,
	CONSTRAINT fk_request_list_import_collection_id_collection FOREIGN KEY(collection_id) REFERENCES collection (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_request_list_import_user ON request_list_import (user_id)")
    op.execute(
        """
CREATE TABLE request_list_item (
	id VARCHAR(26) NOT NULL,
	import_id VARCHAR(26) NOT NULL,
	position INTEGER NOT NULL,
	line_text VARCHAR NOT NULL,
	isbn VARCHAR(13),
	outcome VARCHAR NOT NULL,
	request_id VARCHAR(26),
	work_id VARCHAR(26),
	annotated BOOLEAN DEFAULT 0 NOT NULL,
	CONSTRAINT pk_request_list_item PRIMARY KEY (id),
	CONSTRAINT ck_request_list_item_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_request_list_item_outcome_values CHECK ("outcome" IN ('in_library', 'requested')),
	CONSTRAINT ck_request_list_item_position_min CHECK ("position" >= 0),
	CONSTRAINT ck_request_list_item_line_text_length CHECK (length("line_text") <= 300),
	CONSTRAINT uq_request_list_item_import_id_position UNIQUE (import_id, position),
	CONSTRAINT fk_request_list_item_import_id_request_list_import FOREIGN KEY(import_id) REFERENCES request_list_import (id) ON DELETE CASCADE,
	CONSTRAINT fk_request_list_item_request_id_request FOREIGN KEY(request_id) REFERENCES request (id) ON DELETE SET NULL,
	CONSTRAINT fk_request_list_item_work_id_work FOREIGN KEY(work_id) REFERENCES work (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_request_list_item_request ON request_list_item (request_id)")


def downgrade() -> None:
    op.drop_table("request_list_item")
    op.drop_table("request_list_import")
