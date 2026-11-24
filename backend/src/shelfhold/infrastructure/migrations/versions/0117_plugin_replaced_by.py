"""The successor a plugin's index named (M6 L09b, EF-SRC-030, D15).

`plugin_install` gains `replaced_by`: the key the installed version's index entry named in
`replaced_by`, recorded at the install and the update beside `min_host` — a later host judges a
migration by the index's own word, not by what it remembers of the document. `plugin migrate-to`
accepts no other replacement for the plugin's sources, and never a revoked one.

`plugin_install` is rebuilt by SQLite batch mode, safe because migrations run with
`foreign_keys=OFF` followed by `foreign_key_check` (L02); reflection hands `CURRENT_TIMESTAMP`
back in a form SQLite re-renders with parentheses, so `installed_at` is restated in
`reflect_args` (0111, 0112, 0116). Reversible: the column is dropped, and a migration is made
again from whatever the index says next.

Number: 0117 follows 0116 (the head of this branch: L09b).
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None

_INSTALL = "plugin_install"


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
        if "replaced_by" not in _columns(_INSTALL):  # a database created since the schema has it
            batch.add_column(sa.Column("replaced_by", sa.Text(), nullable=True))


def downgrade() -> None:
    with _batch_install() as batch:
        if "replaced_by" in _columns(_INSTALL):
            batch.drop_column("replaced_by")
