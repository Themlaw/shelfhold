"""The sync point of a Kobo device (M6 L17a, EF-INT-012, D20): `kobo_sync_point`.

One row per device: what its last completed sync gave it (the entitlements and the tags) and when
that was, so the next sync answers only what changed since the device was last seen — and, in the
`in_progress` document, the sync now in flight: what its first page froze (the walk it serves, the
deletions it will announce, the tags, the reading states, the instant of page 1), for the pages
are separate requests and the sync point written at the end must hold what the device was **given**,
not what the last page could see. Created from the schema so the DDL stays identical to
`metadata.create_all` (ENF-OPS-002); nothing existing is touched. Reversible: the downgrade drops
the table, and every device then syncs as if for the first time (a full sync, no data lost).

Number: 0115 is the one the launch of L17a names (0109 to 0114 are held by other lots).
`down_revision` is the head of main (0113, L13).
"""

from alembic import op

from shelfhold.infrastructure.schema import reading

revision = "0115"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    reading.kobo_sync_point.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    reading.kobo_sync_point.drop(op.get_bind(), checkfirst=True)
