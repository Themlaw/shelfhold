"""One open download request per Unit (PLAN-M2 L27, EF-SRC-037, I52).

`download_request.unit_id` names the Unit a `unit` ticket asks for, so that a unique index can say
that a Unit never has two open requests, whatever the Variant and the binding they come from. The
column is filled from the Variant. A Unit that already holds two open requests (it could before
this migration) keeps the one already `downloaded`, else the oldest; the others are cancelled, and
the next cycle asks again if the Unit is still missing. No file and no Variant is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # plain ADD COLUMN: no table rebuild
    op.add_column("download_request", sa.Column("unit_id", sa.String(26), nullable=True))
    op.execute(
        "UPDATE download_request SET unit_id = "
        "(SELECT v.unit_id FROM unit_variant v WHERE v.id = download_request.variant_id) "
        "WHERE target_kind = 'unit' AND variant_id IS NOT NULL"
    )
    op.execute(
        "UPDATE download_request SET state = 'cancelled' WHERE unit_id IS NOT NULL "
        "AND state IN ('requested', 'downloaded') AND EXISTS (SELECT 1 FROM download_request o "
        "WHERE o.unit_id = download_request.unit_id AND o.state IN ('requested', 'downloaded') "
        "AND ((o.state = 'downloaded' AND download_request.state = 'requested') "
        "OR (o.state = download_request.state AND (o.requested_at < download_request.requested_at "
        "OR (o.requested_at = download_request.requested_at AND o.id < download_request.id)))))"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_download_request_open_unit ON download_request (unit_id) "
        "WHERE \"state\" IN ('requested', 'downloaded')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_download_request_open_unit")
    op.drop_column("download_request", "unit_id")
