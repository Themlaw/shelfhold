"""The repositories of signed plugin indexes and the revocations seen (M6 L07, EF-SRC-017).

Two tables, created from the schema so the DDL stays identical to `metadata.create_all`
(ENF-OPS-002). Nothing existing is touched; reversible. No repository is preconfigured: the
tables start empty, and the admin pins one by its URL and the public key of its signer.

Number: 0110 follows 0108 (the head of main when this lot was frozen; 0109 stays with the
1.2.x hotfix).
"""

from alembic import op

from shelfhold.infrastructure.schema import acquisition

revision = "0110"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    acquisition.plugin_repo.create(op.get_bind(), checkfirst=True)
    acquisition.plugin_revocation.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    acquisition.plugin_revocation.drop(op.get_bind(), checkfirst=True)
    acquisition.plugin_repo.drop(op.get_bind(), checkfirst=True)
