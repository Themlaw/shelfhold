"""The registry of copies (`replica`, EF-BIB-045), in a module of its own so that an import
contract can name it: `shelfhold.library.replica` is its only reader (`replica-single-reader`)."""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    bool_column,
    in_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    relative_path_check,
    ulid_check,
    ulid_column,
)

REPLICA_LOCATIONS = ("upstream", "library", "store", "destination", "adopted_ro")
REPLICA_FIDELITIES = ("original", "derived")

# The single registry of copies (EF-BIB-045): `independent_copies(unit)` is the only reader.
# `independent` is false until an inode distinct from every other copy is established, so a
# doubtful copy is never counted. A file is one row: the partial indexes below say so.
replica = sa.Table(
    "replica",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("unit_id", sa.String(26), sa.ForeignKey("unit.id", ondelete="CASCADE")),
    sa.Column("export_item_id", sa.String(26), sa.ForeignKey("export_item.id", ondelete="CASCADE")),
    sa.Column("location", sa.String(), nullable=False),
    sa.Column("physical_root_id", sa.String(26), sa.ForeignKey("physical_root.root_id")),
    sa.Column("rel_path", sa.Text(), nullable=True),
    sa.Column("remote_ref", sa.String(), nullable=True),
    sa.Column("dev", sa.BigInteger(), nullable=True),
    sa.Column("ino", sa.BigInteger(), nullable=True),
    sa.Column("size", sa.BigInteger(), nullable=True),
    sa.Column("mtime_ns", sa.BigInteger(), nullable=True),
    sa.Column("content_hash", sa.String(), nullable=True),
    sa.Column("file_hash", sa.String(), nullable=True),
    bool_column("independent", default=False),
    bool_column("mutable_by_third_party", default=False),  # Kavita, KOReader, the user
    sa.Column("fidelity", sa.String(), nullable=False, server_default="original"),
    sa.Column("verified_at", UtcDateTime(), nullable=True),
    now_column(),
    ulid_check("id"),
    in_check("location", REPLICA_LOCATIONS),
    in_check("fidelity", REPLICA_FIDELITIES),
    prefixed_hash_check("content_hash"),
    prefixed_hash_check("file_hash"),
    positive_check("size"),
    relative_path_check("rel_path"),
    sa.CheckConstraint('"unit_id" IS NOT NULL OR "export_item_id" IS NOT NULL', name="has_owner"),
    sa.CheckConstraint('"rel_path" IS NOT NULL OR "remote_ref" IS NOT NULL', name="is_located"),
    sa.CheckConstraint(
        "\"location\" NOT IN ('library', 'adopted_ro') "
        'OR ("physical_root_id" IS NOT NULL AND "rel_path" IS NOT NULL)',
        name="library_has_root_and_path",
    ),
    sa.Index("ix_replica_unit", "unit_id"),
    sa.Index("ix_replica_export_item", "export_item_id"),
    sa.Index("ix_replica_content_hash", "content_hash"),
    sa.Index("ix_replica_inode", "dev", "ino"),
)
sa.Index(
    "uq_replica_root_path",
    replica.c.physical_root_id,
    replica.c.rel_path,
    unique=True,
    sqlite_where=sa.text('"physical_root_id" IS NOT NULL AND "rel_path" IS NOT NULL'),
)
sa.Index(
    "uq_replica_store_path",
    replica.c.rel_path,
    unique=True,
    sqlite_where=sa.text("\"location\" = 'store'"),
)
