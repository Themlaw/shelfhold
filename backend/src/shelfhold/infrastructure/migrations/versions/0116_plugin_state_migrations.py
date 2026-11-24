"""The state migrations of the plugins and the host's own upgrades (M6 L09b, EF-SRC-030, D16).

`plugin_state` gains `state_written_by_version`: the version of the plugin that wrote each state
document. The rollback is refused, with the reason, toward a version that cannot read the state
the installed one wrote — the host cannot know what an older version reads, so it refuses
whenever the state is newer than the target. `plugin_install` gains `min_host`: the minimum host
version the installed version's index entry declared, recorded at the install and the update, so
the next start-up can judge the plugins a host upgrade left behind (with `api`, out of the
N/N-1 window) and tell `plugin.incompatible` once.

`plugin_install` is rebuilt by SQLite batch mode, safe because migrations run with
`foreign_keys=OFF` followed by `foreign_key_check` (L02); reflection hands `CURRENT_TIMESTAMP`
back in a form SQLite re-renders with parentheses, so `installed_at` is restated in
`reflect_args` (0111, 0112). `plugin_state` only gains a nullable column: a plain `ADD COLUMN`.
Reversible: both columns are dropped, and the judgement of a host upgrade is made again from
whatever the index says next.

Number: 0116 follows 0112 (the head of this branch: L09a).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0112"
branch_labels = None
depends_on = None

_INSTALL = "plugin_install"
_STATE = "plugin_state"


def _columns(table: str) -> set[str]:
    return {row[1] for row in op.get_bind().execute(sa.text(f"PRAGMA table_info({table})"))}


def _batch_install() -> Any:
    """Reflection hands `CURRENT_TIMESTAMP` back in a form SQLite re-renders with parentheses:
    the column is restated so the rebuilt table keeps the DDL `create_all` writes."""
    installed_at = sa.Column(
        "installed_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    return op.batch_alter_table(_INSTALL, recreate="always", reflect_args=(installed_at,))


def upgrade() -> None:
    with _batch_install() as batch:
        if "min_host" not in _columns(_INSTALL):  # a database created since the schema has it
            batch.add_column(sa.Column("min_host", sa.Text(), nullable=True))
    if "state_written_by_version" not in _columns(_STATE):
        op.add_column(
            _STATE, sa.Column("state_written_by_version", sa.Text(), nullable=True)
        )  # fmt: skip


def downgrade() -> None:
    if "state_written_by_version" in _columns(_STATE):
        op.drop_column(_STATE, "state_written_by_version")
    with _batch_install() as batch:
        if "min_host" in _columns(_INSTALL):
            batch.drop_column("min_host")
