"""Normalizer versions of two identity keys and the arrival time (PLAN-M1a-remediation L46).

`edition.lang_norm_by` and `unit_variant.remote_unit_id_norm_by` say which normalizer version
produced the stored value (ENF-OPS-021); `export_record.resolved_at` is when the arrival polling
saw the file in the destination's index (EF-INT-025). Existing rows are filled in: a language
stored with its raw form was produced by `lang@1` (not the `x-unknown` placeholder), and every
identifier is kept as received (`remote_unit_id@1`). A record already `resolved` keeps no arrival
time: it is left out of the median.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


# Rebuilding `unit_variant` drops the table the append-only guard of 0003 reads; the guard goes
# away and comes back around the rebuild (same statement as 0003 and 0004).
VARIANT_REVISION_NO_DELETE = (
    "CREATE TRIGGER variant_revision_no_delete "
    "BEFORE DELETE ON variant_revision "
    "WHEN EXISTS (SELECT 1 FROM unit_variant WHERE id = OLD.variant_id) "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
)


def _now(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )


def _blank(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.String(), server_default=sa.text("''"), nullable=False)


@contextmanager
def _rebuild(table: str) -> Iterator[BatchOperations]:
    """Rebuild in batch mode, restating the defaults reflection would render differently."""
    restated: dict[str, list[sa.Column[Any]]] = {
        "edition": [
            _now("created_at"),
            *(_blank(n) for n in ("publisher", "text_version", "performance", "packaging")),
        ],
        "unit_variant": [_now("first_seen_at")],
    }
    if table == "unit_variant":
        op.execute("DROP TRIGGER variant_revision_no_delete")
    with op.batch_alter_table(
        table, recreate="always", reflect_args=tuple(restated[table])
    ) as batch:
        yield batch
    if table == "unit_variant":
        op.execute(VARIANT_REVISION_NO_DELETE)


def _versioned(column: str) -> str:
    return (
        f'"{column}" IS NULL OR ("{column}" GLOB \'?*@[0-9]*\' AND "{column}" NOT GLOB '
        f"'*@*[^0-9]*' AND \"{column}\" NOT GLOB '*@*@*')"
    )


def upgrade() -> None:
    with _rebuild("edition") as batch:
        batch.add_column(sa.Column("lang_norm_by", sa.String(), nullable=True))
        batch.create_check_constraint(
            op.f("ck_edition_lang_norm_by_versioned"), _versioned("lang_norm_by")
        )
    with _rebuild("unit_variant") as batch:
        batch.add_column(sa.Column("remote_unit_id_norm_by", sa.String(), nullable=True))
        batch.create_check_constraint(
            op.f("ck_unit_variant_remote_unit_id_norm_by_versioned"),
            _versioned("remote_unit_id_norm_by"),
        )
    op.add_column("export_record", sa.Column("resolved_at", sa.DateTime(), nullable=True))
    # `lang@1` only where it produced the value: the `x-unknown` placeholder and a language an
    # adoption stored without its raw form (`lang_raw` NULL) have no normalizer behind them.
    op.execute(
        "UPDATE edition SET lang_norm_by = 'lang@1' "
        "WHERE lang_raw IS NOT NULL AND lang <> 'x-unknown'"
    )
    op.execute("UPDATE unit_variant SET remote_unit_id_norm_by = 'remote_unit_id@1'")


def downgrade() -> None:
    with op.batch_alter_table("export_record") as batch:
        batch.drop_column("resolved_at")
    with _rebuild("unit_variant") as batch:
        batch.drop_constraint(op.f("ck_unit_variant_remote_unit_id_norm_by_versioned"))
        batch.drop_column("remote_unit_id_norm_by")
    with _rebuild("edition") as batch:
        batch.drop_constraint(op.f("ck_edition_lang_norm_by_versioned"))
        batch.drop_column("lang_norm_by")
