"""M3 schema (PLAN-M3 L01): what the family, the library and the operations of M3 need.

Inventory first (docs/decisions/M3.md): 0001 to 0040 already lay down `user.role` (admin, member,
child), `user.theme|color_scheme|title_lang_pref` and `source_config.family_safe` (the `family`
mark of D13, false by default); none of those is created again.

New tables: `profile_source_policy`, `user_state`, `request`, `notification_digest`,
`pending_source_item`, `source_alias_migration`, `merge_log`, `work_redirect`, `thumbnail`,
`time_window`. New columns: `user.max_age_rating|kavita_username|notify_mode|quiet_start|
quiet_end|hide_unrated_remote|subject_filter`, `trash_item.level`,
`unit.integrity_state|integrity_checked_at`, `team.number_offset`,
`instance.maintenance_since|maintenance_by`.

The head of main is 0040: this revision chains on it, and 0041 to 0045 stay free.

Safe defaults: a child may never have a NULL age ceiling (NULL is not "all ages"), so the children
that exist get 0, the strictest ceiling, before the CHECK is laid down. Every other row keeps its
value; the bin entries of before M3 are `delete_content`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0046"
down_revision = "0040"
branch_labels = None
depends_on = None

CLOCK = "{c} IS NULL OR ({c} GLOB '[0-2][0-9]:[0-5][0-9]' AND {c} < '24:00')"
ULID = "length({c}) = 26 AND {c} NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'"
HASH = "length({c}) = 71 AND {c} GLOB 'sha256:*' AND substr({c}, 8) NOT GLOB '*[^0-9a-f]*'"
RELATIVE = (
    "{c} NOT GLOB '/*' AND {c} <> '..' AND {c} NOT GLOB '../*' "
    "AND {c} NOT GLOB '*/../*' AND {c} NOT GLOB '*/..'"
)
CHILD_CEILING = '"role" <> \'child\' OR "max_age_rating" IS NOT NULL'


def _expr(template: str, column: str) -> str:
    return template.format(c=f'"{column}"')


def _now(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )


def _ck(table: str, name: str, expression: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _in(table: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    listing = ", ".join(f"'{v}'" for v in values)
    return _ck(table, f"{column}_values", f'"{column}" IN ({listing})')


def _json(table: str, column: str, shape: str) -> sa.CheckConstraint:
    label = "list" if shape == "array" else "object"
    return _ck(
        table,
        f"{column}_{label}",
        f'json_valid("{column}") AND json_type("{column}") = \'{shape}\'',
    )


def _fk(column: str, target: str, table: str, ondelete: str) -> sa.ForeignKeyConstraint:
    parent = target.split(".")[0]
    return sa.ForeignKeyConstraint(
        [column],
        [target],
        name=op.f(f"fk_{table}_{column}_{parent}"),
        ondelete=ondelete,
    )


@contextmanager
def _rebuild(table: str, restated: tuple[sa.Column[Any], ...] = ()) -> Iterator[BatchOperations]:
    """Rebuild in batch mode (a CHECK cannot be added in place). The indexes and triggers of the
    table are read first and laid down again as they were: a rebuild would lose a partial index
    and every trigger."""
    bind = op.get_bind()
    saved = bind.execute(
        sa.text(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = :t AND type IN ('index', 'trigger') AND sql IS NOT NULL"
        ),
        {"t": table},
    ).all()
    for kind, name, _ in saved:
        if kind == "trigger":
            op.execute(f'DROP TRIGGER "{name}"')
    with op.batch_alter_table(table, recreate="always", reflect_args=restated) as batch:
        yield batch
    for kind, name, statement in saved:
        if kind == "index":
            op.execute(f'DROP INDEX IF EXISTS "{name}"')
        op.execute(statement)


STAMP = '"stamp"'


def _user_restated() -> tuple[sa.Column[Any], ...]:
    """What reflection would render differently: the clock default and the NOCASE collation."""
    return (
        _now("created_at"),
        sa.Column("username", sa.String(collation="NOCASE"), nullable=False),
    )


def _unit_restated() -> tuple[sa.Column[Any], ...]:
    """Reflection renders the empty-string defaults as `('')`."""
    blanks = (
        sa.Column(n, sa.String(), server_default=sa.text("''"), nullable=False)
        for n in ("part", "number_segment")
    )
    return (_now("created_at"), *blanks)


def upgrade() -> None:
    _tables()
    with _rebuild("user", _user_restated()) as batch:
        batch.add_column(sa.Column("max_age_rating", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("kavita_username", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("notify_mode", sa.String(), server_default="digest", nullable=False)
        )
        batch.add_column(sa.Column("quiet_start", sa.String(), nullable=True))
        batch.add_column(sa.Column("quiet_end", sa.String(), nullable=True))
        batch.add_column(
            sa.Column("hide_unrated_remote", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch.add_column(
            sa.Column("subject_filter", sa.JSON(), server_default="[]", nullable=False)
        )
        batch.create_check_constraint(
            op.f("ck_user_notify_mode_values"), "\"notify_mode\" IN ('digest', 'immediate')"
        )
        batch.create_check_constraint(
            op.f("ck_user_max_age_rating_min_age"),
            '"max_age_rating" IS NULL OR (typeof("max_age_rating") = \'integer\' '
            'AND "max_age_rating" BETWEEN 0 AND 99)',
        )
        batch.create_check_constraint(
            op.f("ck_user_kavita_username_not_empty"),
            '"kavita_username" IS NULL OR "kavita_username" <> \'\'',
        )
        batch.create_check_constraint(
            op.f("ck_user_quiet_start_clock"), _expr(CLOCK, "quiet_start")
        )
        batch.create_check_constraint(op.f("ck_user_quiet_end_clock"), _expr(CLOCK, "quiet_end"))
        batch.create_check_constraint(
            op.f("ck_user_quiet_hours_both_or_none"),
            '("quiet_start" IS NULL) = ("quiet_end" IS NULL)',
        )
        batch.create_check_constraint(
            op.f("ck_user_subject_filter_list"),
            'json_valid("subject_filter") AND json_type("subject_filter") = \'array\'',
        )
        batch.create_unique_constraint(op.f("uq_user_kavita_username"), ["kavita_username"])
    # A child that exists keeps the strictest ceiling: NULL is never "all ages".
    op.execute("UPDATE \"user\" SET max_age_rating = 0 WHERE role = 'child'")
    with _rebuild("user", _user_restated()) as batch:
        batch.create_check_constraint(op.f("ck_user_child_has_age_ceiling"), CHILD_CEILING)

    with _rebuild("trash_item") as batch:
        batch.add_column(
            sa.Column("level", sa.String(), server_default="delete_content", nullable=False)
        )
        batch.create_check_constraint(
            op.f("ck_trash_item_level_values"),
            "\"level\" IN ('unfollow', 'remove_destinations', 'delete_content')",
        )
    with _rebuild("unit", _unit_restated()) as batch:
        batch.add_column(
            sa.Column("integrity_state", sa.String(), server_default="unchecked", nullable=False)
        )
        batch.add_column(sa.Column("integrity_checked_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            op.f("ck_unit_integrity_state_values"),
            "\"integrity_state\" IN ('unchecked', 'ok', 'corrupt')",
        )
    # `team.number_offset` (EF-BIB-028, may be cut): laid down, no behaviour.
    with _rebuild("team", (_now("created_at"),)) as batch:
        batch.add_column(
            sa.Column("number_offset", sa.Integer(), server_default="0", nullable=False)
        )
        batch.create_check_constraint(
            op.f("ck_team_number_offset_integer"), "typeof(\"number_offset\") = 'integer'"
        )
    with _rebuild("instance", (_now("created_at"),)) as batch:
        batch.add_column(sa.Column("maintenance_since", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("maintenance_by", sa.String(length=26), nullable=True))
        batch.create_foreign_key(
            op.f("fk_instance_maintenance_by_user"),
            "user",
            ["maintenance_by"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            op.f("ck_instance_maintenance_by_has_since"),
            '"maintenance_by" IS NULL OR "maintenance_since" IS NOT NULL',
        )


def _tables() -> None:
    op.create_table(
        "profile_source_policy",
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("policy", sa.String(), nullable=False),
        _now("updated_at"),
        _in("profile_source_policy", "policy", ("add_directly", "request", "forbidden")),
        _ck("profile_source_policy", "source_key_not_empty", "\"source_key\" <> ''"),
        _fk("user_id", "user.id", "profile_source_policy", "CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "source_key", name=op.f("pk_profile_source_policy")),
    )
    op.create_table(
        "user_state",
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("work_id", sa.String(length=26), nullable=False),
        sa.Column("followed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("favorite", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        _now("stamp"),
        _fk("user_id", "user.id", "user_state", "CASCADE"),
        _fk("work_id", "work.id", "user_state", "CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "work_id", name=op.f("pk_user_state")),
    )
    op.create_index(op.f("ix_user_state_work"), "user_state", ["work_id"])
    op.create_table(
        "request",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("requester_user_id", sa.String(length=26), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_value", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("reject_reason", sa.String(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        _ck("request", "id_ulid", _expr(ULID, "id")),
        _in("request", "target_kind", ("work_id", "source_ref", "isbn", "free_text")),
        _in(
            "request",
            "state",
            ("pending", "approved", "rejected", "not_found", "cancelled", "arrived"),
        ),
        _ck("request", "target_value_not_empty", "\"target_value\" <> ''"),
        _ck(
            "request",
            "work_target_is_ulid",
            '"target_kind" <> \'work_id\' OR (length("target_value") = 26 '
            "AND \"target_value\" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*')",
        ),
        _ck(
            "request",
            "reason_only_on_refusal",
            '"reject_reason" IS NULL OR "state" = \'rejected\'',
        ),
        _fk("requester_user_id", "user.id", "request", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_request")),
    )
    op.create_index(op.f("ix_request_requester"), "request", ["requester_user_id", "state"])
    op.create_table(
        "notification_digest",
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), server_default="{}", nullable=False),
        _json("notification_digest", "payload", "object"),
        _fk("user_id", "user.id", "notification_digest", "CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "due_at", name=op.f("pk_notification_digest")),
    )
    op.create_table(
        "pending_source_item",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("series_ref", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        _ck("pending_source_item", "id_ulid", _expr(ULID, "id")),
        _ck(
            "pending_source_item",
            "named",
            "\"source_key\" <> '' AND \"series_ref\" <> '' AND \"title\" <> ''",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_source_item")),
        sa.UniqueConstraint(
            "source_key", "series_ref", name=op.f("uq_pending_source_item_source_key_series_ref")
        ),
    )
    op.create_table(
        "source_alias_migration",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("from_scheme", sa.String(), nullable=False),
        sa.Column("to_scheme", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="previewed", nullable=False),
        sa.Column("preview", sa.JSON(), server_default="{}", nullable=False),
        _now("created_at"),
        _ck("source_alias_migration", "id_ulid", _expr(ULID, "id")),
        _in("source_alias_migration", "state", ("previewed", "applied", "cancelled")),
        _json("source_alias_migration", "preview", "object"),
        _ck("source_alias_migration", "schemes_differ", '"from_scheme" <> "to_scheme"'),
        _ck("source_alias_migration", "source_key_not_empty", "\"source_key\" <> ''"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_alias_migration")),
    )
    op.create_index(
        op.f("ix_source_alias_migration_source"), "source_alias_migration", ["source_key"]
    )
    op.create_table(
        "merge_log",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("survivor_id", sa.String(length=26), nullable=False),
        sa.Column("absorbed_id", sa.String(length=26), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        _now("created_at"),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
        _ck("merge_log", "id_ulid", _expr(ULID, "id")),
        _ck("merge_log", "survivor_id_ulid", _expr(ULID, "survivor_id")),
        _ck("merge_log", "absorbed_id_ulid", _expr(ULID, "absorbed_id")),
        _in("merge_log", "kind", ("merge", "split")),
        _json("merge_log", "snapshot", "object"),
        _ck("merge_log", "distinct_works", '"survivor_id" <> "absorbed_id"'),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merge_log")),
    )
    op.create_index(op.f("ix_merge_log_survivor"), "merge_log", ["survivor_id"])
    op.create_index(op.f("ix_merge_log_absorbed"), "merge_log", ["absorbed_id"])
    op.create_table(
        "work_redirect",
        sa.Column("from_work_id", sa.String(length=26), nullable=False),
        sa.Column("to_work_id", sa.String(length=26), nullable=False),
        sa.Column("merge_log_id", sa.String(length=26), nullable=True),
        _now("created_at"),
        _ck("work_redirect", "from_work_id_ulid", _expr(ULID, "from_work_id")),
        _ck("work_redirect", "no_self_redirect", '"from_work_id" <> "to_work_id"'),
        _fk("to_work_id", "work.id", "work_redirect", "CASCADE"),
        _fk("merge_log_id", "merge_log.id", "work_redirect", "SET NULL"),
        sa.PrimaryKeyConstraint("from_work_id", name=op.f("pk_work_redirect")),
    )
    op.create_index(op.f("ix_work_redirect_target"), "work_redirect", ["to_work_id"])
    op.create_table(
        "thumbnail",
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        _now("created_at"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        _ck("thumbnail", "content_hash_sha256_prefixed", _expr(HASH, "content_hash")),
        _ck("thumbnail", "size_values", '"size" IN (160, 320, 640)'),
        _ck("thumbnail", "path_relative", _expr(RELATIVE, "path")),
        _ck("thumbnail", "bytes_min", '"bytes" >= 0'),
        sa.PrimaryKeyConstraint("content_hash", "size", name=op.f("pk_thumbnail")),
    )
    op.create_index(op.f("ix_thumbnail_last_used"), "thumbnail", ["last_used_at"])
    op.create_table(
        "time_window",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("start_utc", sa.String(), nullable=False),
        sa.Column("end_utc", sa.String(), nullable=False),
        sa.Column("tz", sa.String(), nullable=False),
        _ck("time_window", "id_ulid", _expr(ULID, "id")),
        _ck("time_window", "start_utc_clock", _expr(CLOCK, "start_utc")),
        _ck("time_window", "end_utc_clock", _expr(CLOCK, "end_utc")),
        _ck("time_window", "scope_not_empty", "\"scope\" <> ''"),
        _ck("time_window", "tz_not_empty", "\"tz\" <> ''"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_window")),
    )
    op.create_index(op.f("ix_time_window_scope"), "time_window", ["scope"])


def downgrade() -> None:
    for table in (
        "time_window",
        "thumbnail",
        "work_redirect",
        "merge_log",
        "source_alias_migration",
        "pending_source_item",
        "notification_digest",
        "request",
        "user_state",
        "profile_source_policy",
    ):
        op.drop_table(table)
    with _rebuild("instance", (_now("created_at"),)) as batch:
        batch.drop_constraint(op.f("ck_instance_maintenance_by_has_since"), type_="check")
        batch.drop_constraint(op.f("fk_instance_maintenance_by_user"), type_="foreignkey")
        batch.drop_column("maintenance_by")
        batch.drop_column("maintenance_since")
    with _rebuild("team", (_now("created_at"),)) as batch:
        batch.drop_constraint(op.f("ck_team_number_offset_integer"), type_="check")
        batch.drop_column("number_offset")
    with _rebuild("unit", _unit_restated()) as batch:
        batch.drop_constraint(op.f("ck_unit_integrity_state_values"), type_="check")
        batch.drop_column("integrity_checked_at")
        batch.drop_column("integrity_state")
    with _rebuild("trash_item") as batch:
        batch.drop_constraint(op.f("ck_trash_item_level_values"), type_="check")
        batch.drop_column("level")
    with _rebuild("user", _user_restated()) as batch:
        batch.drop_constraint(op.f("ck_user_child_has_age_ceiling"), type_="check")
        batch.drop_constraint(op.f("uq_user_kavita_username"), type_="unique")
        for name in (
            "subject_filter_list",
            "quiet_hours_both_or_none",
            "quiet_end_clock",
            "quiet_start_clock",
            "kavita_username_not_empty",
            "max_age_rating_min_age",
            "notify_mode_values",
        ):
            batch.drop_constraint(op.f(f"ck_user_{name}"), type_="check")
        for column in (
            "subject_filter",
            "hide_unrated_remote",
            "quiet_end",
            "quiet_start",
            "notify_mode",
            "kavita_username",
            "max_age_rating",
        ):
            batch.drop_column(column)
