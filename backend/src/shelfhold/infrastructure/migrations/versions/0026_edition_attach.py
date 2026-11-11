"""External identifiers append-only, and the journal of attaches by identifier (PLAN-M2 L12).

`external_id` gets three triggers: no UPDATE but the mark of a renumbering (`current` ->
`superseded`, origin kept or set to `legacy`), no DELETE, no `INSERT OR REPLACE`. No row is
rewritten. New table `edition_attach` (EF-BIB-003, EF-BIB-037, EF-BIB-055): what an attach by
external identifier changed, so that "Séparer" undoes it exactly, and the attaches that wait for
an admin's confirmation.

The number: 0021 to 0024 are reserved by PLAN-M2 §2 (L16, L17, L25, L27) and 0025 is the
reopening of the age suggestions (#126); `down_revision` is the head of `main` at the rebase.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0021"
branch_labels = None
depends_on = None

APPEND_ONLY = "BEGIN SELECT RAISE(ABORT, 'external_id is append-only'); END"
TRIGGERS = {
    "external_id_no_update": (
        "CREATE TRIGGER external_id_no_update "
        "BEFORE UPDATE ON external_id "
        "WHEN OLD.entity_type IS NOT NEW.entity_type OR OLD.entity_id IS NOT NEW.entity_id "
        "OR OLD.provider IS NOT NEW.provider OR OLD.value IS NOT NEW.value "
        "OR OLD.id_scheme IS NOT NEW.id_scheme OR OLD.created_at IS NOT NEW.created_at "
        "OR OLD.state IS NOT 'current' OR NEW.state IS NOT 'superseded' "
        "OR (NEW.origin IS NOT OLD.origin AND NEW.origin IS NOT 'legacy') " + APPEND_ONLY
    ),
    "external_id_no_delete": (
        "CREATE TRIGGER external_id_no_delete BEFORE DELETE ON external_id "
        "BEGIN SELECT RAISE(ABORT, 'external_id is append-only'); END"
    ),
    "external_id_no_replace": (
        "CREATE TRIGGER external_id_no_replace "
        "BEFORE INSERT ON external_id "
        "WHEN EXISTS (SELECT 1 FROM external_id WHERE entity_type = NEW.entity_type "
        "AND provider = NEW.provider AND value = NEW.value) "
        "BEGIN SELECT RAISE(ABORT, 'external_id is append-only'); END"
    ),
}


def upgrade() -> None:
    for statement in TRIGGERS.values():
        op.execute(statement)
    op.create_table(
        "edition_attach",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("from_work_id", sa.String(length=26), nullable=False),
        sa.Column("to_work_id", sa.String(length=26), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("prior", sa.JSON(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_edition_attach_id_ulid"),
        ),
        sa.CheckConstraint(
            "\"state\" IN ('suggested', 'attached', 'undone', 'declined')",
            name=op.f("ck_edition_attach_state_values"),
        ),
        sa.CheckConstraint(
            "\"reason\" IN ('external_id', 'age_raise')",
            name=op.f("ck_edition_attach_reason_values"),
        ),
        sa.CheckConstraint(
            'json_valid("prior") AND json_type("prior") = \'object\'',
            name=op.f("ck_edition_attach_prior_object"),
        ),
        sa.CheckConstraint(
            '"from_work_id" <> "to_work_id"', name=op.f("ck_edition_attach_distinct_works")
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_edition_attach_edition_id_edition"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_work_id"],
            ["work.id"],
            name=op.f("fk_edition_attach_from_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_work_id"],
            ["work.id"],
            name=op.f("fk_edition_attach_to_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edition_attach")),
    )
    op.create_index("ix_edition_attach_edition", "edition_attach", ["edition_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_edition_attach_edition", table_name="edition_attach")
    op.drop_table("edition_attach")
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
