"""« Soutenir l'auteur » (M4 L23, EF-BIB-032): official read/buy/licensed links.

One table, `external_link`: the links an enrichment hands over, with their provenance,
never an affiliate parameter. Created from the schema so the DDL stays identical to
`metadata.create_all` (ENF-OPS-002).
"""

from alembic import op

from shelfhold.infrastructure.schema import catalog

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    catalog.external_link.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    catalog.external_link.drop(op.get_bind(), checkfirst=True)
