"""What a collection needs to be pushed to Kavita (M4 L18b, EF-EXP-025).

- `collection.public_approved_at`: the admin's word that a profile's private list may be promoted in
  Kavita (NULL: not said; the list stays under the service account, out of every other account's
  sight);
- `collection_remote`, rebuilt: it loses its foreign key to `collection` and gains `note`. The note
  is what Shelfhold put in Kavita, written before each call (the series it added, the title it asked
  for while the identifier is not known, what the last complete push said). It must outlive the
  collection: with `ON DELETE CASCADE`, deleting a collection would forget what it left in Kavita,
  and a process killed at any step could leave something that nothing points to. Rows are kept.

Nothing else is rewritten: a collection made before this migration reads as not approved, a remote
row as having nothing noted. Reversible (the downgrade puts the foreign key back and drops the
notes: what they described is then no longer known to Shelfhold).

Number: 0093 follows 0092 (L31), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("collection", sa.Column("public_approved_at", sa.DateTime(), nullable=True))
    op.execute("ALTER TABLE collection_remote RENAME TO collection_remote_old")
    op.execute(
        """
CREATE TABLE collection_remote (
	collection_id VARCHAR(26) NOT NULL,
	target_id VARCHAR(26) NOT NULL,
	remote_id VARCHAR NOT NULL,
	synced_at DATETIME,
	note JSON DEFAULT '{}' NOT NULL,
	CONSTRAINT pk_collection_remote PRIMARY KEY (collection_id, target_id),
	CONSTRAINT ck_collection_remote_remote_id_named CHECK ("remote_id" <> ''),
	CONSTRAINT fk_collection_remote_target_id_export_target FOREIGN KEY(target_id) REFERENCES export_target (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        "INSERT INTO collection_remote (collection_id, target_id, remote_id, synced_at) "
        "SELECT collection_id, target_id, remote_id, synced_at FROM collection_remote_old"
    )
    op.execute("DROP TABLE collection_remote_old")


def downgrade() -> None:
    op.execute("ALTER TABLE collection_remote RENAME TO collection_remote_new")
    op.execute(
        """
CREATE TABLE collection_remote (
	collection_id VARCHAR(26) NOT NULL,
	target_id VARCHAR(26) NOT NULL,
	remote_id VARCHAR NOT NULL,
	synced_at DATETIME,
	CONSTRAINT pk_collection_remote PRIMARY KEY (collection_id, target_id),
	CONSTRAINT ck_collection_remote_remote_id_named CHECK ("remote_id" <> ''),
	CONSTRAINT fk_collection_remote_collection_id_collection FOREIGN KEY(collection_id) REFERENCES collection (id) ON DELETE CASCADE,
	CONSTRAINT fk_collection_remote_target_id_export_target FOREIGN KEY(target_id) REFERENCES export_target (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        "INSERT INTO collection_remote (collection_id, target_id, remote_id, synced_at) "
        "SELECT n.collection_id, n.target_id, n.remote_id, n.synced_at FROM collection_remote_new n "
        "JOIN collection c ON c.id = n.collection_id WHERE n.remote_id <> 'pending'"
    )
    op.execute("DROP TABLE collection_remote_new")
    op.drop_column("collection", "public_approved_at")
