"""Storage tables: contents kept in /store, the trash, the registry of copies (02 section 3.2).

A file is either only filed in /library (passthrough), kept in /store, or referenced in place
(adopted). Which hash says what: `original_sha256` = the bytes received, `content_hash` = the
normalized payload (`content_hash()`, versioned, EF-BIB-048), `derived_hash` = the neutralized
derivative (EF-BIB-051). Every hash is `sha256:<hex>`.
"""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    bool_column,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    relative_path_check,
    ulid_check,
    ulid_column,
)
from shelfhold.infrastructure.schema.catalog import MEDIA, norm_by_check

RETENTIONS = ("passthrough", "keep", "adopted_ref")
STORE_PROVENANCES = ("received", "extracted", "extracted_derived")
TRASH_KINDS = ("unit", "work", "export_file")
TRASH_LEVELS = ("unfollow", "remove_destinations", "delete_content")
THUMBNAIL_SIZES = (160, 320, 640)
REPLICA_LOCATIONS = ("upstream", "library", "store", "destination", "adopted_ro")
REPLICA_FIDELITIES = ("original", "derived")
WATERMARK_ORIGINS = ("none", "heuristic", "user")
IMPORT_STATES = ("seen", "stored", "refused", "removed")


def processor_check(column: str) -> sa.CheckConstraint:
    """`processor@version`, e.g. `xhtml-neutralizer@v1`."""
    c = f'"{column}"'
    return sa.CheckConstraint(
        f"{c} IS NULL OR ({c} GLOB '?*@?*' AND {c} NOT GLOB '*@*@*')", name=f"{column}_versioned"
    )


# 0..N per Variant (an EPUB and a PDF), one per media type. The Variant is only missing for a
# file adopted in place. Erasing a Unit or a Variant removes its contents first, files included:
# nothing here cascades, or a file would be forgotten on disk (EF-BIB-052).
content = sa.Table(
    "content",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("unit_id", sa.String(26), sa.ForeignKey("unit.id"), nullable=True),
    sa.Column("variant_id", sa.String(26), sa.ForeignKey("unit_variant.id"), nullable=True),
    sa.Column("kind", sa.String(), nullable=False),
    sa.Column("media_type", sa.String(), nullable=False),  # MIME
    sa.Column("format", sa.String(), nullable=False),  # cbz, epub, pdf, m4b...
    bool_column("watermarked", default=False),
    sa.Column("watermark_origin", sa.String(), nullable=False, server_default="none"),
    sa.Column("retention", sa.String(), nullable=False),
    sa.Column("path", sa.Text(), nullable=True),  # relative to /store, only when kept
    sa.Column("original_sha256", sa.String(), nullable=False),
    sa.Column("content_hash", sa.String(), nullable=True),
    sa.Column("content_hash_by", sa.String(), nullable=True),  # `content_hash()` version
    sa.Column("store_provenance", sa.String(), nullable=False),
    sa.Column("derived_hash", sa.String(), nullable=True),
    sa.Column("derived_by", sa.String(), nullable=True),
    sa.Column("derived_hash_by", sa.String(), nullable=True),  # M2: neutralizer security updates
    sa.Column("derived_chain", sa.JSON(), nullable=False, server_default="[]"),  # M2
    sa.Column("size", sa.BigInteger(), nullable=False),
    sa.Column("page_count", sa.Integer(), nullable=True),
    now_column(),
    sa.ForeignKeyConstraint(
        ["variant_id", "unit_id"], ["unit_variant.id", "unit_variant.unit_id"]
    ),  # a content names the Unit of its own Variant (NULL unit: a file that carries several)
    ulid_check("id"),
    in_check("kind", MEDIA),
    in_check("retention", RETENTIONS),
    in_check("watermark_origin", WATERMARK_ORIGINS),
    in_check("store_provenance", STORE_PROVENANCES),
    sa.CheckConstraint("\"media_type\" GLOB '?*/?*'", name="media_type_mime"),
    sa.CheckConstraint(
        "\"format\" <> '' AND \"format\" NOT GLOB '*[^a-z0-9]*'", name="format_slug"
    ),
    sa.CheckConstraint('("retention" = \'keep\') = ("path" IS NOT NULL)', name="path_iff_keep"),
    relative_path_check("path"),
    sa.CheckConstraint(
        '"variant_id" IS NOT NULL OR "retention" = \'adopted_ref\'',
        name="variant_unless_adopted",
    ),
    prefixed_hash_check("original_sha256"),
    prefixed_hash_check("content_hash"),
    prefixed_hash_check("derived_hash"),
    norm_by_check("content_hash_by"),
    norm_by_check("derived_hash_by"),
    processor_check("derived_by"),
    sa.CheckConstraint('("derived_hash" IS NULL) = ("derived_by" IS NULL)', name="derived_pair"),
    sa.CheckConstraint(
        '"store_provenance" <> \'extracted_derived\' OR "derived_hash" IS NOT NULL',
        name="extracted_derived_has_derived_hash",
    ),
    json_shape_check("derived_chain", "array"),
    positive_check("size"),
    positive_check("page_count"),
    sa.UniqueConstraint("variant_id", "media_type"),
    sa.Index("ix_content_unit", "unit_id"),
    sa.Index("ix_content_content_hash", "content_hash"),
)

# The bin of one physical root: `<root>/.shelfhold/recycle/` (EF-EXP-059). Emptying it and the
# early purge under disk pressure never touch an irreplaceable Unit (EF-BIB-030).
trash_item = sa.Table(
    "trash_item",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("kind", sa.String(), nullable=False),
    ulid_column("ref"),  # the Unit, the Work or the export item that was binned
    sa.Column("path", sa.Text(), nullable=False),  # relative to the recycle directory
    sa.Column(
        "physical_root_id", sa.String(26), sa.ForeignKey("physical_root.root_id"), nullable=True
    ),  # NULL: the bin of /store
    sa.Column("size", sa.BigInteger(), nullable=True),
    sa.Column("deleted_at", UtcDateTime(), nullable=False),
    sa.Column("purge_after", UtcDateTime(), nullable=False),  # 30 days (ENF-OPS-009)
    # Level of the deletion that put it here (EF-BIB-021); what a bin entry held before M3 was
    # a deleted content.
    sa.Column("level", sa.String(), nullable=False, server_default="delete_content"),
    ulid_check("id"),
    ulid_check("ref"),
    in_check("kind", TRASH_KINDS),
    in_check("level", TRASH_LEVELS),
    relative_path_check("path"),
    positive_check("size"),
    sa.CheckConstraint('"purge_after" >= "deleted_at"', name="purge_after_deleted_at"),
    sa.CheckConstraint(
        '"kind" <> \'export_file\' OR "physical_root_id" IS NOT NULL',
        name="export_file_has_root",
    ),
    sa.Index("ix_trash_item_purge_after", "purge_after"),
    sa.Index("ix_trash_item_ref", "kind", "ref"),
)

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

# What "Mettre à l'abri" could not take in its last cycle (EF-BIB-042 (4), EF-INT-023): one row, the
# Units and bytes that stayed behind the per-cycle cap or the room of /store. `units = 0` means the
# queue is empty. The Units themselves are not listed here: they are the ones owed a copy and
# without one (a read of the database), so a lost row can only be rebuilt by the next cycle.
safeguard_backlog = sa.Table(
    "safeguard_backlog",
    metadata,
    sa.Column("name", sa.String(), primary_key=True),
    sa.Column("units", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
    sa.Column("since", UtcDateTime(), nullable=True),
    sa.Column(
        "updated_at", UtcDateTime(), nullable=False, server_default=sa.func.current_timestamp()
    ),
    sa.CheckConstraint("\"name\" = 'store'", name="name_values"),
    positive_check("units"),
    positive_check("bytes"),
)

# What the import folder held when last scanned, and what was decided about each file (EF-EXP-065).
# `rel_path` is relative to the import folder; `sha256` is filled once the file is read.
import_entry = sa.Table(
    "import_entry",
    metadata,
    sa.Column("rel_path", sa.Text(), primary_key=True),
    sa.Column("size", sa.BigInteger(), nullable=False),
    sa.Column("mtime", UtcDateTime(), nullable=False),
    sa.Column("sha256", sa.String(), nullable=True),
    sa.Column("state", sa.String(), nullable=False, server_default="seen"),
    sa.Column("reason", sa.String(), nullable=True),
    sa.Column("first_seen_at", UtcDateTime(), nullable=False),
    sa.Column("decided_at", UtcDateTime(), nullable=True),
    relative_path_check("rel_path"),
    prefixed_hash_check("sha256"),
    in_check("state", IMPORT_STATES),
    positive_check("size"),
    sa.Index("ix_import_entry_state", "state"),
)

# A cover thumbnail, addressed by the hash of its source (EF-BIB-020); generated lazily.
thumbnail = sa.Table(
    "thumbnail",
    metadata,
    sa.Column("content_hash", sa.String(), nullable=False),
    sa.Column("size", sa.Integer(), nullable=False),
    sa.Column("path", sa.Text(), nullable=False),  # relative to the thumbnail directory
    sa.Column("bytes", sa.BigInteger(), nullable=False),
    now_column(),
    sa.Column("last_used_at", UtcDateTime(), nullable=True),
    sa.PrimaryKeyConstraint("content_hash", "size"),
    prefixed_hash_check("content_hash"),
    sa.CheckConstraint(
        '"size" IN (' + ", ".join(str(n) for n in THUMBNAIL_SIZES) + ")", name="size_values"
    ),
    relative_path_check("path"),
    positive_check("bytes"),
    sa.Index("ix_thumbnail_last_used", "last_used_at"),
)
