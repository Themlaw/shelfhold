"""The sealed keys of the metadata providers (M4 L21a, EF-BIB-013, ENF-SEC-005).

One table, `provider_secret`: one row per provider (Hardcover, Google Books), the key in the four
sealed columns of the secret vault and nowhere else. Created from the schema so the DDL stays
identical to `metadata.create_all` (ENF-OPS-002). Nothing existing is touched; reversible.

Number: 0088 follows 0087 (L30), the head of main when this lot was frozen.
"""

from alembic import op

from shelfhold.infrastructure.schema import acquisition

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    acquisition.provider_secret.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    acquisition.provider_secret.drop(op.get_bind(), checkfirst=True)
