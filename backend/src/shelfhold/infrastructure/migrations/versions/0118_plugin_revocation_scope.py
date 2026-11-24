"""A revocation binds the pair (repository, signer) that published it (M6 closure review, §3.1).

`plugin_revocation` was global: an index B could revoke a plugin installed from an index A. A
revocation now carries the repository and the signer's key it was seen under, both part of the
key, and binds only the installs pinned to that same pair. The old rows say nothing about their
source, so they cannot be attributed and are dropped (the product is not in production): the
next index the repository publishes lists its revocations again, and a refresh keeps them.

Number: 0118 follows 0117 (the head of this branch).
"""

import sqlalchemy as sa
from alembic import op

from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.schema._base import NAMING_CONVENTION, in_check, now_column

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None

_TABLE = "plugin_revocation"


def _columns() -> set[str]:
    return {row[1] for row in op.get_bind().execute(sa.text(f"PRAGMA table_info({_TABLE})"))}


def upgrade() -> None:
    if "index_url" in _columns():  # a database created since the schema has it
        return
    acquisition.plugin_revocation.drop(op.get_bind(), checkfirst=True)
    acquisition.plugin_revocation.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    if "index_url" not in _columns():
        return
    acquisition.plugin_revocation.drop(op.get_bind(), checkfirst=True)
    before = sa.Table(
        _TABLE,
        sa.MetaData(naming_convention=NAMING_CONVENTION),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("value", sa.Text(), primary_key=True),
        now_column("seen_at"),
        in_check("kind", acquisition.REVOCATION_KINDS),
        sa.CheckConstraint("\"value\" <> ''", name="value_not_empty"),
    )
    before.create(op.get_bind())
