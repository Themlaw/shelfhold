"""The schema the lots of M4 share (M4 L02): trees and indexers, member accounts, sync cursor.

- `tree` and `tree_indexer` (ADR-0052): one folder written once, read by one or more servers;
- `destination_account` (a member's account and sealed token on a destination) and
  `progress_sync_cursor` (where the progress synchronisation of each account stopped);
- `document_alias.shelf_destination_id` and `kavita_md5`: the alias table of M1a is extended, not
  recreated, and every old row stays (bound to no destination);
- two triggers: a remote library is linked once per shelf.

No row is created for the trees of existing bindings: the render profile comes from the plugin's
capabilities and the template, which SQL does not see; the lot that renders owns that backfill.
The other tables of M4 are created by the lot that uses them, in the migration reserved for it.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

_LINKED_ON_SHELF = (
    "EXISTS (SELECT 1 FROM shelf_destination_remote_library AS l "
    "JOIN shelf_destination AS d ON d.id = l.shelf_destination_id "
    "JOIN shelf_destination AS n ON n.id = NEW.shelf_destination_id "
    "WHERE l.remote_library_id = NEW.remote_library_id AND d.shelf_id = n.shelf_id%s)"
)
_ALREADY_LINKED = "SELECT RAISE(ABORT, 'a remote library is already linked on this shelf');"
_OTHER_SHELF = (
    "EXISTS (SELECT 1 FROM tree_indexer AS o "
    "JOIN shelf_destination AS old_d ON old_d.id = o.shelf_destination_id "
    "JOIN shelf_destination AS new_d ON new_d.id = NEW.shelf_destination_id "
    "WHERE o.tree_id = NEW.tree_id AND old_d.shelf_id <> new_d.shelf_id)"
)
_NOT_A_TREE = (
    "(SELECT transport FROM shelf_destination WHERE id = NEW.shelf_destination_id) <> 'tree'"
)
_TRIGGERS: dict[str, str] = {
    "link_one_library_per_shelf_insert": (
        f"BEFORE INSERT ON shelf_destination_remote_library WHEN {_LINKED_ON_SHELF % ''} "
        f"BEGIN {_ALREADY_LINKED} END"
    ),
    "link_one_library_per_shelf_update": (
        "BEFORE UPDATE OF shelf_destination_id, remote_library_id "
        "ON shelf_destination_remote_library WHEN "
        + _LINKED_ON_SHELF
        % (
            " AND NOT (l.shelf_destination_id = OLD.shelf_destination_id "
            "AND l.remote_library_id = OLD.remote_library_id)"
        )
        + f" BEGIN {_ALREADY_LINKED} END"
    ),
}
for _event, _of in (("INSERT", ""), ("UPDATE", " OF tree_id, shelf_destination_id")):
    _TRIGGERS[f"tree_indexer_same_shelf_{_event.lower()}"] = (
        f"BEFORE {_event}{_of} ON tree_indexer WHEN {_OTHER_SHELF} BEGIN "
        "SELECT RAISE(ABORT, 'tree_indexer: the indexers of a tree belong to the same shelf'); END"
    )
    _TRIGGERS[f"tree_indexer_tree_transport_{_event.lower()}"] = (
        f"BEFORE {_event}{_of} ON tree_indexer WHEN {_NOT_A_TREE} BEGIN "
        "SELECT RAISE(ABORT, 'tree_indexer: only a tree transport has a tree'); END"
    )
_TRIGGERS["tree_indexer_keep_primary"] = (
    "BEFORE DELETE ON tree_indexer WHEN OLD.role = 'primary' "
    "AND EXISTS (SELECT 1 FROM tree WHERE id = OLD.tree_id) "
    "AND EXISTS (SELECT 1 FROM tree_indexer AS o WHERE o.tree_id = OLD.tree_id "
    "AND o.shelf_destination_id <> OLD.shelf_destination_id) "
    "BEGIN SELECT RAISE(ABORT, 'tree_indexer: the primary still has secondaries'); END"
)
_LINK_TRIGGERS = ("link_one_library_per_shelf_insert", "link_one_library_per_shelf_update")


def _ulid(column: str) -> str:
    return f'length("{column}") = 26 AND "{column}" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\''


def _md5(column: str) -> str:
    c = f'"{column}"'
    return f"length({c}) = 36 AND {c} GLOB 'md5:*' AND substr({c}, 5) NOT GLOB '*[^0-9a-f]*'"


def _clock() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )


def _restated() -> tuple[sa.Column[Any], ...]:
    """What reflection would render differently: the clock default."""
    return (_clock(),)


def upgrade() -> None:
    op.create_table(
        "tree",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("root_id", sa.String(length=26), nullable=False),
        sa.Column("root_subpath", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("render_profile", sa.JSON(), server_default="{}", nullable=False),
        _clock(),
        sa.CheckConstraint(_ulid("id"), name=op.f("ck_tree_id_ulid")),
        sa.CheckConstraint(
            "\"root_subpath\" NOT GLOB '/*' AND \"root_subpath\" <> '..' "
            "AND \"root_subpath\" NOT GLOB '../*' AND \"root_subpath\" NOT GLOB '*/../*' "
            "AND \"root_subpath\" NOT GLOB '*/..'",
            name=op.f("ck_tree_root_subpath_relative"),
        ),
        sa.CheckConstraint(
            'json_valid("render_profile") AND json_type("render_profile") = \'object\'',
            name=op.f("ck_tree_render_profile_object"),
        ),
        sa.ForeignKeyConstraint(
            ["root_id"], ["physical_root.root_id"], name=op.f("fk_tree_root_id_physical_root")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tree")),
        sa.UniqueConstraint("root_id", "root_subpath", name=op.f("uq_tree_root_id_root_subpath")),
    )
    op.create_table(
        "tree_indexer",
        sa.Column("tree_id", sa.String(length=26), nullable=False),
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("target_id", sa.String(length=26), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        _clock(),
        sa.CheckConstraint(
            "\"role\" IN ('primary', 'secondary')", name=op.f("ck_tree_indexer_role_values")
        ),
        sa.ForeignKeyConstraint(
            ["tree_id"],
            ["tree.id"],
            name=op.f("fk_tree_indexer_tree_id_tree"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shelf_destination_id", "target_id"],
            ["shelf_destination.id", "shelf_destination.target_id"],
            name=op.f("fk_tree_indexer_shelf_destination_id_target_id_shelf_destination"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("shelf_destination_id", name=op.f("pk_tree_indexer")),
        sa.UniqueConstraint("tree_id", "target_id", name=op.f("uq_tree_indexer_tree_id_target_id")),
    )
    op.create_index(
        "uq_tree_indexer_primary",
        "tree_indexer",
        ["tree_id"],
        unique=True,
        sqlite_where=sa.text("\"role\" = 'primary'"),
    )
    op.create_table(
        "destination_account",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("remote_user", sa.String(), nullable=False),
        sa.Column("secret_kid", sa.String(), nullable=True),
        sa.Column("secret_v", sa.Integer(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ct", sa.LargeBinary(), nullable=True),
        sa.Column("token_fingerprint", sa.String(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        _clock(),
        sa.CheckConstraint(_ulid("id"), name=op.f("ck_destination_account_id_ulid")),
        sa.CheckConstraint(
            '("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL '
            'AND "secret_ct" IS NULL) '
            'OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 '
            'AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL '
            'AND length("secret_ct") >= 16)',
            name=op.f("ck_destination_account_secret_sealed"),
        ),
        sa.CheckConstraint(
            "\"remote_user\" <> ''", name=op.f("ck_destination_account_remote_user_not_empty")
        ),
        sa.CheckConstraint(
            '"revoked_at" IS NOT NULL OR "secret_ct" IS NOT NULL',
            name=op.f("ck_destination_account_live_has_token"),
        ),
        sa.CheckConstraint(
            '("token_fingerprint" IS NULL) = ("secret_ct" IS NULL)',
            name=op.f("ck_destination_account_fingerprint_iff_sealed"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_destination_account_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shelf_destination_id"],
            ["shelf_destination.id"],
            name=op.f("fk_destination_account_shelf_destination_id_shelf_destination"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["secret_kid"],
            ["secret_key.kid"],
            name=op.f("fk_destination_account_secret_kid_secret_key"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_destination_account")),
        sa.UniqueConstraint(
            "user_id",
            "shelf_destination_id",
            name=op.f("uq_destination_account_user_id_shelf_destination_id"),
        ),
        sa.UniqueConstraint(
            "shelf_destination_id",
            "remote_user",
            name=op.f("uq_destination_account_shelf_destination_id_remote_user"),
        ),
    )
    op.create_table(
        "progress_sync_cursor",
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("last_pull_server_ts", sa.DateTime(), nullable=True),
        sa.Column("last_push_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id", "shelf_destination_id"],
            ["destination_account.user_id", "destination_account.shelf_destination_id"],
            name=op.f("fk_progress_sync_cursor_user_id_shelf_destination_id_destination_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "shelf_destination_id", "user_id", name=op.f("pk_progress_sync_cursor")
        ),
    )
    with op.batch_alter_table(
        "document_alias", recreate="always", reflect_args=_restated()
    ) as batch:
        batch.add_column(sa.Column("shelf_destination_id", sa.String(26), nullable=True))
        batch.add_column(sa.Column("kavita_md5", sa.String(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_document_alias_shelf_destination_id_shelf_destination"),
            "shelf_destination",
            ["shelf_destination_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            op.f("ck_document_alias_kavita_md5_md5_prefixed"), _md5("kavita_md5")
        )
        batch.create_index("ix_document_alias_kavita_md5", ["kavita_md5"])
        batch.create_index("ix_document_alias_destination", ["shelf_destination_id"])
    for name, body in _TRIGGERS.items():
        op.execute(f"CREATE TRIGGER {name} {body}")


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    with op.batch_alter_table(
        "document_alias", recreate="always", reflect_args=_restated()
    ) as batch:
        batch.drop_index("ix_document_alias_destination")
        batch.drop_index("ix_document_alias_kavita_md5")
        batch.drop_constraint(op.f("ck_document_alias_kavita_md5_md5_prefixed"), type_="check")
        batch.drop_constraint(
            op.f("fk_document_alias_shelf_destination_id_shelf_destination"), type_="foreignkey"
        )
        batch.drop_column("kavita_md5")
        batch.drop_column("shelf_destination_id")
    op.drop_table("progress_sync_cursor")
    op.drop_table("destination_account")
    op.drop_index("uq_tree_indexer_primary", table_name="tree_indexer")
    op.drop_table("tree_indexer")
    op.drop_table("tree")
