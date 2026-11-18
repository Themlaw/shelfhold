"""Export tables: destinations, shelves, exported items, manifest, remote series (02 section 3.3).

Conventions of this module:
- an exported file (`export_item`) is independent of any destination; the manifest
  (`export_record`) is keyed by `(shelf_destination, export_item)` (EF-EXP-058);
- a remote library has a local ULID, so two servers never collide on a remote identifier, and
  `remote_ref` is only a cache re-resolved by path (EF-EXP-039, EF-EXP-062);
- composite foreign keys carry the "same parent" invariants: a destination only links libraries
  of its own server, a series belongs to a library its destination is linked to, an Edition's
  series belongs to the library of that link.
"""

import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    add_trigger,
    bool_column,
    in_check,
    json_shape_check,
    json_valid_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    relative_path_check,
    ulid_check,
    ulid_column,
)
from shelfhold.infrastructure.schema.acquisition import sealed_secret_columns
from shelfhold.infrastructure.schema.catalog import lang_check, norm_by_check

ACCESS_PARTITIONINGS = ("none", "library", "user")
FORMAT_POLICIES = ("cbz_per_chapter", "per_volume")
RETENTION_MODES = ("passthrough", "keep")
LINK_MODES = ("copy", "hardlink")
TRANSPORTS = ("tree", "dropbox", "push", "pull")
EGRESSES = ("foyer", "externe")
IMPORT_MODES = ("copy", "reflink", "hardlink")
DESTINATION_MODES = ("normal", "degraded_readonly", "degraded_feature")
# `bundle` is the nameless volume of a range (EF-BIB-044); `opds_bundle` carries the aliases of a
# part of a series block served on OPDS (M4 L49a) and is no volume: no export plan ever reads it.
EXPORT_ITEM_KINDS = ("unit", "volume", "bundle", "adopted", "opds_bundle")
CARRIER_TYPES = ("content", "export_item", "volume", "unit")  # `unit`: re-cut chapters (M2)
COVERAGE_EXTENTS = ("full", "partial")
COVERAGE_ORIGINS = ("comicinfo", "filename", "source_name", "provider", "manual")
LOCATOR_TYPES = ("path", "remote_id", "idempotency_key", "none")
RECORD_STATES = (
    "planned", "written", "scanned", "resolved", "adopted", "missing", "recycled", "error",
)  # fmt: skip
SERIES_ASSIGNERS = ("first_export", "plan")
INDEXER_ROLES = ("primary", "secondary")


def _age_ceiling(column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f'"{column}" IS NULL OR (typeof("{column}") = \'integer\' AND "{column}" BETWEEN 0 AND 99)',
        name=f"{column}_min_age",
    )


# A destination of the catalog: one Kavita server in M1a, others later (Destinations describe,
# the core executes). Secrets (API key) are one sealed document, never a plain column.
export_target = sa.Table(
    "export_target",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("kind", sa.String(), nullable=False),
    sa.Column("name", sa.String(), nullable=False),
    sa.Column("access_partitioning", sa.String(), nullable=False, server_default="none"),
    # M6: a third-party plugin's declared value is only a default until the admin attests it
    sa.Column("access_partitioning_attested_at", UtcDateTime(), nullable=True),
    sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),  # nothing secret
    *sealed_secret_columns(),
    # [{"local": "/library/manga", "remote": "/manga", "mode": "rw" | "adopt_ro"}, ...]
    sa.Column("path_map", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("version_seen", sa.String(), nullable=True),
    sa.Column("health", sa.JSON(), nullable=True),
    now_column(),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    sa.CheckConstraint("\"kind\" <> '' AND \"kind\" NOT GLOB '*[^a-z0-9_]*'", name="kind_slug"),
    sa.CheckConstraint("\"name\" <> ''", name="name_not_empty"),
    in_check("access_partitioning", ACCESS_PARTITIONINGS),
    json_shape_check("config", "object"),
    json_shape_check("path_map", "array"),
    sa.UniqueConstraint("name"),
)

# Detects a reinstall of the server (EF-EXP-039): URL + creation ids and dates of its libraries.
kavita_instance = sa.Table(
    "kavita_instance",
    metadata,
    sa.Column(
        "target_id",
        sa.String(26),
        sa.ForeignKey("export_target.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("fingerprint", sa.String(), nullable=False),
    sa.Column("version", sa.String(), nullable=True),
    sa.Column("seen_at", UtcDateTime(), nullable=False),
)

# The place where a kind of Work goes. `max_age_rating` is the ceiling (ordinal, X is above
# every age); NULL = no ceiling, an adult shelf. `library_id` is the parental-control scope
# (ADR-0027), an opaque key. `follow_suspended`: Works of an adopted child library (EF-EXP-048).
shelf = sa.Table(
    "shelf",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("name", sa.String(), nullable=False),
    sa.Column("phrase", sa.String(), nullable=False),
    sa.Column("default_kinds", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("format_policy", sa.String(), nullable=False, server_default="cbz_per_chapter"),
    sa.Column("library_id", sa.String(), nullable=True),
    sa.Column("max_age_rating", sa.Integer(), nullable=True),
    sa.Column("allowed_profiles", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("retention_mode", sa.String(), nullable=False, server_default="passthrough"),
    bool_column("purge_upstream_after_export", default=False),
    bool_column("hold_until_rated", default=False),
    # A chapter already read is not replaced by a better copy (EF-BIB-040).
    bool_column("skip_read_upgrades", default=True),
    # How a file reaches the library from the store: copied, or hard-linked (EF-EXP-015).
    sa.Column("link_mode", sa.String(), nullable=False, server_default="copy"),
    sa.Column("paused_reason", sa.String(), nullable=True),
    sa.Column("follow_suspended", sa.JSON(), nullable=False, server_default="[]"),
    now_column(),
    ulid_check("id"),
    sa.CheckConstraint("\"name\" <> ''", name="name_not_empty"),
    in_check("format_policy", FORMAT_POLICIES),
    in_check("retention_mode", RETENTION_MODES),
    in_check("link_mode", LINK_MODES),
    _age_ceiling("max_age_rating"),
    json_shape_check("default_kinds", "array"),
    json_shape_check("allowed_profiles", "array"),
    json_shape_check("follow_suspended", "array"),
    sa.UniqueConstraint("name"),
)

# Shelf x Destination. The links to remote libraries are in `shelf_destination_remote_library`.
# `root_subpath` is the stable key; the template is copied at creation, never followed.
shelf_destination = sa.Table(
    "shelf_destination",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("shelf_id", sa.String(26), sa.ForeignKey("shelf.id"), nullable=False),
    sa.Column("target_id", sa.String(26), sa.ForeignKey("export_target.id"), nullable=False),
    sa.Column("transport", sa.String(), nullable=False),  # copied from the capabilities
    sa.Column("egress", sa.String(), nullable=False),  # attested per instance
    sa.Column("root_subpath", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("template", sa.Text(), nullable=False),
    sa.Column("template_origin", sa.String(), nullable=False),  # default@<version>
    sa.Column("import_mode", sa.String(), nullable=False, server_default="copy"),
    sa.Column("kavita_library_type", sa.String(), nullable=True),
    sa.Column("mode", sa.String(), nullable=False, server_default="normal"),
    bool_column("adopted", default=False),
    sa.Column("pre_adoption_settings", sa.JSON(none_as_null=True), nullable=True),
    bool_column("sync_progress", default=False),  # M4
    bool_column("kavita_series_age_locked", default=False),  # M2
    bool_column("enabled", default=True),
    now_column(),
    ulid_check("id"),
    in_check("transport", TRANSPORTS),
    in_check("egress", EGRESSES),
    in_check("import_mode", IMPORT_MODES),
    in_check("mode", DESTINATION_MODES),
    sa.CheckConstraint(
        "\"template\" <> '' AND \"template_origin\" <> ''", name="template_not_empty"
    ),
    sa.UniqueConstraint("shelf_id", "target_id", "root_subpath"),
    # target of the composite key that keeps a linked library on the destination's own server
    sa.UniqueConstraint("id", "target_id"),
    sa.Index("ix_shelf_destination_target", "target_id"),
)

remote_library = sa.Table(
    "remote_library",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("export_target_id", sa.String(26), sa.ForeignKey("export_target.id"), nullable=False),
    sa.Column("remote_ref", sa.String(), nullable=False),  # cache, re-resolved by path
    sa.Column("kind_seen", sa.String(), nullable=True),
    sa.Column("seen_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    sa.CheckConstraint("\"remote_ref\" <> ''", name="remote_ref_not_empty"),
    sa.UniqueConstraint("export_target_id", "remote_ref"),
    sa.UniqueConstraint("id", "export_target_id"),
)

shelf_destination_remote_library = sa.Table(
    "shelf_destination_remote_library",
    metadata,
    sa.Column("shelf_destination_id", sa.String(26), nullable=False),
    sa.Column("remote_library_id", sa.String(26), nullable=False),
    sa.Column("export_target_id", sa.String(26), nullable=False),
    sa.ForeignKeyConstraint(
        ["shelf_destination_id", "export_target_id"],
        ["shelf_destination.id", "shelf_destination.target_id"],
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["remote_library_id", "export_target_id"],
        ["remote_library.id", "remote_library.export_target_id"],
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint("shelf_destination_id", "remote_library_id"),
    sa.Index("ix_shelf_destination_remote_library_library", "remote_library_id"),
)

# A remote library is linked once per shelf (EF-EXP-026, ADR-0052 T6): SQLite cannot state a
# UNIQUE across the join with `shelf_destination`, and a `shelf_id` copied into the link would
# break every insertion point, so two triggers say it.
_LINKED_ON_SHELF = (
    "EXISTS (SELECT 1 FROM shelf_destination_remote_library AS l "
    "JOIN shelf_destination AS d ON d.id = l.shelf_destination_id "
    "JOIN shelf_destination AS n ON n.id = NEW.shelf_destination_id "
    "WHERE l.remote_library_id = NEW.remote_library_id AND d.shelf_id = n.shelf_id%s)"
)
_ALREADY_LINKED = "SELECT RAISE(ABORT, 'a remote library is already linked on this shelf');"
add_trigger(
    shelf_destination_remote_library,
    "link_one_library_per_shelf_insert",
    f"BEFORE INSERT ON shelf_destination_remote_library WHEN {_LINKED_ON_SHELF % ''} "
    f"BEGIN {_ALREADY_LINKED} END",
)
add_trigger(
    shelf_destination_remote_library,
    "link_one_library_per_shelf_update",
    "BEFORE UPDATE OF shelf_destination_id, remote_library_id ON shelf_destination_remote_library "
    "WHEN "
    + _LINKED_ON_SHELF
    % (
        " AND NOT (l.shelf_destination_id = OLD.shelf_destination_id "
        "AND l.remote_library_id = OLD.remote_library_id)"
    )
    + f" BEGIN {_ALREADY_LINKED} END",
)

# Last read-only audit of the accounts that reach a library of the server (EF-EXP-055):
# `age_restriction` and `include_unknowns` are the loosest values among those accounts.
kavita_access_audit = sa.Table(
    "kavita_access_audit",
    metadata,
    sa.Column(
        "remote_library_id",
        sa.String(26),
        sa.ForeignKey("remote_library.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("accounts", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("age_restriction", sa.Integer(), nullable=True),
    sa.Column("include_unknowns", sa.Boolean(), nullable=True),
    sa.Column("exposed_to_children", sa.Boolean(), nullable=False),
    sa.Column("kavita_version", sa.String(), nullable=True),
    sa.Column("audited_at", UtcDateTime(), nullable=False),
    json_shape_check("accounts", "array"),
    _age_ceiling("age_restriction"),
)

# A folder Shelfhold writes, identified by (physical root, normalized subpath): ADR-0052 T1. The
# sentinel, the lease and the provenance mirror stay per root (ADR-0022, ADR-0046). `render_profile`
# is the common rendering every indexer accepts (two levels, naming, sidecars, formats). A root
# that holds trees is not dropped silently.
tree = sa.Table(
    "tree",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("root_id", sa.String(26), sa.ForeignKey("physical_root.root_id"), nullable=False),
    sa.Column("root_subpath", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("render_profile", sa.JSON(), nullable=False, server_default="{}"),
    now_column(),
    ulid_check("id"),
    relative_path_check("root_subpath"),
    json_shape_check("render_profile", "object"),
    sa.UniqueConstraint("root_id", "root_subpath"),
)

# A binding attached to a tree. `primary` made the tree and renders its plan; `secondary` only
# indexes it. SQL states: one tree per binding, one primary per tree, one indexer per server
# instance and tree (T9), a primary with secondaries is not removed. The same-shelf rule (T2) and
# the `tree` transport are triggers. `target_id` is the binding's own, kept by a composite key.
tree_indexer = sa.Table(
    "tree_indexer",
    metadata,
    sa.Column(
        "tree_id", sa.String(26), sa.ForeignKey("tree.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("shelf_destination_id", sa.String(26), nullable=False),
    sa.Column("target_id", sa.String(26), nullable=False),
    sa.Column("role", sa.String(), nullable=False),
    now_column(),
    sa.ForeignKeyConstraint(
        ["shelf_destination_id", "target_id"],
        ["shelf_destination.id", "shelf_destination.target_id"],
        ondelete="RESTRICT",
    ),
    sa.PrimaryKeyConstraint("shelf_destination_id"),
    in_check("role", INDEXER_ROLES),
    sa.UniqueConstraint("tree_id", "target_id"),
)
sa.Index(
    "uq_tree_indexer_primary",
    tree_indexer.c.tree_id,
    unique=True,
    sqlite_where=sa.text("\"role\" = 'primary'"),
)
_OTHER_SHELF = (
    "EXISTS (SELECT 1 FROM tree_indexer AS o "
    "JOIN shelf_destination AS old_d ON old_d.id = o.shelf_destination_id "
    "JOIN shelf_destination AS new_d ON new_d.id = NEW.shelf_destination_id "
    "WHERE o.tree_id = NEW.tree_id AND old_d.shelf_id <> new_d.shelf_id)"
)
_NOT_A_TREE = (
    "(SELECT transport FROM shelf_destination WHERE id = NEW.shelf_destination_id) <> 'tree'"
)
for _event, _of in (("INSERT", ""), ("UPDATE", " OF tree_id, shelf_destination_id")):
    add_trigger(
        tree_indexer,
        f"tree_indexer_same_shelf_{_event.lower()}",
        f"BEFORE {_event}{_of} ON tree_indexer WHEN {_OTHER_SHELF} BEGIN "
        "SELECT RAISE(ABORT, 'tree_indexer: the indexers of a tree belong to the same shelf'); END",
    )
    add_trigger(
        tree_indexer,
        f"tree_indexer_tree_transport_{_event.lower()}",
        f"BEFORE {_event}{_of} ON tree_indexer WHEN {_NOT_A_TREE} BEGIN "
        "SELECT RAISE(ABORT, 'tree_indexer: only a tree transport has a tree'); END",
    )
del _event, _of
# Detach the secondaries first (ADR-0052 cycle de vie). Deleting the tree itself is not blocked:
# its row is gone when the cascade deletes the indexers.
add_trigger(
    tree_indexer,
    "tree_indexer_keep_primary",
    "BEFORE DELETE ON tree_indexer WHEN OLD.role = 'primary' "
    "AND EXISTS (SELECT 1 FROM tree WHERE id = OLD.tree_id) "
    "AND EXISTS (SELECT 1 FROM tree_indexer AS o WHERE o.tree_id = OLD.tree_id "
    "AND o.shelf_destination_id <> OLD.shelf_destination_id) "
    "BEGIN SELECT RAISE(ABORT, 'tree_indexer: the primary still has secondaries'); END",
)

# A file or object exported, independent of the destination (F1). It covers one or more Units
# through `unit_coverage`; Shelfhold-written files are 1-1 (invariant), adopted volumes are N.
export_item = sa.Table(
    "export_item",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("edition_id", sa.String(26), sa.ForeignKey("edition.id"), nullable=False),
    sa.Column("kind", sa.String(), nullable=False),
    # series, track, number_norm, part, template_id@version: frozen at first export (I22)
    sa.Column("frozen_attrs", sa.JSON(), nullable=False, server_default="{}"),
    bool_column("closed", default=False),
    sa.Column("content_id", sa.String(26), sa.ForeignKey("content.id", ondelete="SET NULL")),
    now_column(),
    ulid_check("id"),
    in_check("kind", EXPORT_ITEM_KINDS),
    json_shape_check("frozen_attrs", "object"),
    sa.Index("ix_export_item_edition", "edition_id"),
    sa.Index("ix_export_item_content", "content_id"),
)

# Support -> Units, N-N. The carrier is polymorphic (no foreign key, the type says where to
# look); triggers below drop a carrier's coverage with it. `start_locator` / `end_locator`:
# the Unit's bounds in the carrier (page index, or href / spine index), NULL when unknown.
unit_coverage = sa.Table(
    "unit_coverage",
    metadata,
    sa.Column("carrier_type", sa.String(), nullable=False),
    sa.Column("carrier_id", sa.String(26), nullable=False),
    sa.Column(
        "unit_id", sa.String(26), sa.ForeignKey("unit.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.Column("start_locator", sa.JSON(none_as_null=True), nullable=True),
    sa.Column("end_locator", sa.JSON(none_as_null=True), nullable=True),
    sa.Column("extent", sa.String(), nullable=False, server_default="full"),
    sa.Column("origin", sa.String(), nullable=False),
    sa.Column("confidence", sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint("carrier_type", "carrier_id", "unit_id"),
    ulid_check("carrier_id"),
    in_check("carrier_type", CARRIER_TYPES),
    in_check("extent", COVERAGE_EXTENTS),
    in_check("origin", COVERAGE_ORIGINS),
    positive_check("position"),
    # an invalid document would break `reading_progress` (json_extract) for every reader
    json_valid_check("start_locator"),
    json_valid_check("end_locator"),
    sa.CheckConstraint(
        '"confidence" IS NULL OR "confidence" BETWEEN 0 AND 1', name="confidence_range"
    ),
    sa.UniqueConstraint("carrier_type", "carrier_id", "position"),
    sa.Index("ix_unit_coverage_unit", "unit_id"),
)


def _coverage_cleanup(table: sa.Table, carrier: str) -> None:
    add_trigger(
        table,
        f"{table.name}_coverage_cleanup",
        f"AFTER DELETE ON {table.name} "  # noqa: S608 (names of this module, no input)
        f"BEGIN DELETE FROM unit_coverage "
        f"WHERE carrier_type = '{carrier}' AND carrier_id = OLD.id; END",
    )


for _carrier_table, _carrier in (
    (export_item, "export_item"),
    (storage.content, "content"),
    (catalog.volume, "volume"),
    (catalog.unit, "unit"),
):
    _coverage_cleanup(_carrier_table, _carrier)
del _carrier_table, _carrier

# The manifest: what was written, where, for which destination. `rel_path` is the resolution key
# of a `tree` destination (EF-EXP-039): unique among live records, a recycled path may be reused.
# `mtime_ns` is the filesystem's nanosecond mtime, for an exact in-place-change check
# (EF-EXP-043). Exactly one locator column is filled, the one `locator_type` names.
export_record = sa.Table(
    "export_record",
    metadata,
    sa.Column(
        "shelf_destination_id", sa.String(26), sa.ForeignKey("shelf_destination.id"), nullable=False
    ),
    sa.Column(
        "export_item_id",
        sa.String(26),
        sa.ForeignKey("export_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("locator_type", sa.String(), nullable=False),
    sa.Column("rel_path", sa.Text(), nullable=True),
    sa.Column("remote_id", sa.String(), nullable=True),
    sa.Column("idempotency_key", sa.String(), nullable=True),
    sa.Column("template_ref", sa.String(), nullable=True),  # template_id@version
    sa.Column("format", sa.String(), nullable=True),
    sa.Column("size", sa.BigInteger(), nullable=True),
    sa.Column("mtime_ns", sa.BigInteger(), nullable=True),
    sa.Column("content_hash", sa.String(), nullable=True),  # the payload as received
    sa.Column("sidecar_sha256", sa.String(), nullable=True),
    sa.Column("file_hash", sa.String(), nullable=True),  # the file rendered for this destination
    # reading-order items of the EPUB served, NULL = unknown (the guard of EF-BIB-024, M2 L26)
    sa.Column("spine_count", sa.Integer(), nullable=True),
    sa.Column("remote_ref", sa.String(), nullable=True),  # cache, purged if the server changes
    sa.Column("written_by_shelfhold_at", UtcDateTime(), nullable=True),
    sa.Column("exported_at", UtcDateTime(), nullable=True),
    # When Kavita's scan was seen to hold the file (set by the arrival polling, EF-INT-025)
    sa.Column("resolved_at", UtcDateTime(), nullable=True),
    sa.Column("state", sa.String(), nullable=False, server_default="planned"),
    sa.Column("error_code", sa.String(), nullable=True),
    now_column(),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    sa.PrimaryKeyConstraint("shelf_destination_id", "export_item_id"),
    in_check("locator_type", LOCATOR_TYPES),
    in_check("state", RECORD_STATES),
    sa.CheckConstraint(
        '("locator_type" = \'path\' AND "rel_path" IS NOT NULL AND "remote_id" IS NULL '
        'AND "idempotency_key" IS NULL) '
        'OR ("locator_type" = \'remote_id\' AND "remote_id" IS NOT NULL AND "rel_path" IS NULL '
        'AND "idempotency_key" IS NULL) '
        'OR ("locator_type" = \'idempotency_key\' AND "idempotency_key" IS NOT NULL '
        'AND "rel_path" IS NULL AND "remote_id" IS NULL) '
        'OR ("locator_type" = \'none\' AND "rel_path" IS NULL AND "remote_id" IS NULL '
        'AND "idempotency_key" IS NULL)',
        name="locator_coherent",
    ),
    relative_path_check("rel_path"),
    prefixed_hash_check("content_hash"),
    prefixed_hash_check("sidecar_sha256"),
    prefixed_hash_check("file_hash"),
    positive_check("size"),
    sa.Index("ix_export_record_item", "export_item_id"),
    sa.Index("ix_export_record_file_hash", "file_hash"),
)
sa.Index(
    "uq_export_record_path",
    export_record.c.shelf_destination_id,
    export_record.c.rel_path,
    unique=True,
    sqlite_where=sa.text('"rel_path" IS NOT NULL AND "state" <> \'recycled\''),
)

# The series of a remote library, with an identity of its own (EF-EXP-062): assigned at first
# export, never recomputed from a name. For an adopted series the `frozen_*` values are re-read
# before every export, never recomputed from the source (EF-EXP-052).
export_series = sa.Table(
    "export_series",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("shelf_destination_id", sa.String(26), nullable=False),
    sa.Column("remote_library_id", sa.String(26), nullable=False),
    sa.Column("proposal_key", sa.JSON(none_as_null=True), nullable=True),
    sa.Column("normalized_name", sa.String(), nullable=False),
    sa.Column("name_norm_by", sa.String(), nullable=True),
    sa.Column("name_raw", sa.String(), nullable=False),
    sa.Column("kavita_series_id", sa.String(), nullable=True),  # cache
    sa.Column("frozen_series", sa.String(), nullable=True),
    sa.Column("frozen_localized_series", sa.String(), nullable=True),
    sa.Column("frozen_series_sort", sa.String(), nullable=True),
    sa.Column("frozen_language", sa.String(), nullable=True),
    sa.Column("has_volume", sa.Boolean(), nullable=True),
    sa.Column("series_dir", sa.String(), nullable=True),
    sa.Column("frozen_at", UtcDateTime(), nullable=True),
    sa.Column("paused_reason", sa.String(), nullable=True),  # `foreign_writer` (EF-EXP-043)
    now_column(),
    sa.ForeignKeyConstraint(
        ["shelf_destination_id", "remote_library_id"],
        [
            "shelf_destination_remote_library.shelf_destination_id",
            "shelf_destination_remote_library.remote_library_id",
        ],
    ),
    ulid_check("id"),
    sa.CheckConstraint("\"normalized_name\" <> ''", name="normalized_name_not_empty"),
    norm_by_check("name_norm_by"),
    lang_check("frozen_language", allow_wildcards=False),
    sa.CheckConstraint(
        '("frozen_series" IS NULL) = ("frozen_at" IS NULL)', name="frozen_with_date"
    ),
    sa.UniqueConstraint("remote_library_id", "normalized_name"),
    sa.UniqueConstraint("id", "remote_library_id"),
    sa.UniqueConstraint("id", "shelf_destination_id"),
)

# Which series an Edition is exported to, per library. A merge, a split or a re-attachment only
# changes this link, by an explicit reassignment proposed in a plan (EF-EXP-062).
edition_export_series = sa.Table(
    "edition_export_series",
    metadata,
    sa.Column("edition_id", sa.String(26), sa.ForeignKey("edition.id"), nullable=False),
    sa.Column(
        "remote_library_id", sa.String(26), sa.ForeignKey("remote_library.id"), nullable=False
    ),
    sa.Column("export_series_id", sa.String(26), nullable=False),
    sa.Column("assigned_at", UtcDateTime(), nullable=False),
    sa.Column("assigned_by", sa.String(), nullable=False),
    sa.ForeignKeyConstraint(
        ["export_series_id", "remote_library_id"],
        ["export_series.id", "export_series.remote_library_id"],
    ),
    sa.PrimaryKeyConstraint("edition_id", "remote_library_id"),
    in_check("assigned_by", SERIES_ASSIGNERS),
    sa.Index("ix_edition_export_series_series", "export_series_id"),
)

# Fields Kavita holds locked BY Shelfhold: only those are rewritten, a difference is drift.
# `pushed_at` NULL = invalidated, to be pushed again (reinstall of the server, EF-EXP-039).
field_owner = sa.Table(
    "field_owner",
    metadata,
    sa.Column("shelf_destination_id", sa.String(26), nullable=False),
    sa.Column("export_series_id", sa.String(26), nullable=False),
    sa.Column("field", sa.String(), nullable=False),
    sa.Column("last_value", sa.JSON(), nullable=False),
    sa.Column("pushed_at", UtcDateTime(), nullable=True),
    sa.ForeignKeyConstraint(
        ["export_series_id", "shelf_destination_id"],
        ["export_series.id", "export_series.shelf_destination_id"],
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint("shelf_destination_id", "export_series_id", "field"),
    sa.CheckConstraint("\"field\" <> ''", name="field_not_empty"),
)

# One row per rewrite of an exported file (EF-LEC-009): a hash of a volume resolves one carrier.
# Table in the schema from M1a, filled from M4, used in M5.
document_alias = sa.Table(
    "document_alias",
    metadata,
    sa.Column(
        "export_item_id",
        sa.String(26),
        sa.ForeignKey("export_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("file_hash", sa.String(), nullable=False),
    sa.Column("koreader_partial_md5", sa.String(), nullable=True),
    sa.Column("content_hash", sa.String(), nullable=False),
    now_column(),
    # M4 (0064): the binding the hashes were computed for, and the hash Kavita keeps. The alias
    # outlives the binding: the old hash still resolves the file.
    sa.Column(
        "shelf_destination_id",
        sa.String(26),
        sa.ForeignKey("shelf_destination.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("kavita_md5", sa.String(), nullable=True),
    sa.PrimaryKeyConstraint("export_item_id", "file_hash"),
    prefixed_hash_check("file_hash"),
    prefixed_hash_check("content_hash"),
    prefixed_hash_check("koreader_partial_md5", "md5", 32),
    prefixed_hash_check("kavita_md5", "md5", 32),
    sa.Index("ix_document_alias_file_hash", "file_hash"),
    sa.Index("ix_document_alias_md5", "koreader_partial_md5"),
    sa.Index("ix_document_alias_kavita_md5", "kavita_md5"),
    sa.Index("ix_document_alias_destination", "shelf_destination_id"),
)

# EF-EXP-016: why a served file was replaced at its path. `restore` puts a binned version back.
# There is no `revision` here: a revision recorded before M2 is never applied by an upgrade
# (EF-BIB-043, the queue of L17).
UPGRADE_REASONS = ("better_version", "preferred_team", "integrity_reacquire", "restore")
UPGRADE_STATES = ("done", "restored")

# One same-path replacement of an exported file (EF-EXP-016): what was served before, what is
# served now and the bin entry (`trash_item`) of the old version, which `restore` puts back. The
# row is written in the transaction that closes the `upgrade` intent, so it exists iff the swap
# did. `koreader_reset`: a new file at the same path is a new document for KOReader (D21).
export_upgrade = sa.Table(
    "export_upgrade",
    metadata,
    ulid_column("id", primary_key=True),
    ulid_column("intent_id"),  # the `upgrade` intent; no key, the journal is pruned
    sa.Column(
        "shelf_destination_id",
        sa.String(26),
        sa.ForeignKey("shelf_destination.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "export_item_id",
        sa.String(26),
        sa.ForeignKey("export_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "unit_id", sa.String(26), sa.ForeignKey("unit.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("reason", sa.String(), nullable=False),
    bool_column("automatic", default=True),
    bool_column("koreader_reset", default=True),
    sa.Column("old_content_id", sa.String(26), sa.ForeignKey("content.id", ondelete="SET NULL")),
    sa.Column("old_content_hash", sa.String(), nullable=True),
    sa.Column("old_sidecar_sha256", sa.String(), nullable=True),
    sa.Column("old_file_hash", sa.String(), nullable=False),
    sa.Column("new_file_hash", sa.String(), nullable=False),
    sa.Column("trash_item_id", sa.String(26), sa.ForeignKey("trash_item.id", ondelete="SET NULL")),
    sa.Column("state", sa.String(), nullable=False, server_default="done"),
    now_column(),
    sa.Column("restored_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    ulid_check("intent_id"),
    in_check("reason", UPGRADE_REASONS),
    in_check("state", UPGRADE_STATES),
    prefixed_hash_check("old_content_hash"),
    prefixed_hash_check("old_sidecar_sha256"),
    prefixed_hash_check("old_file_hash"),
    prefixed_hash_check("new_file_hash"),
    sa.CheckConstraint(
        '("state" = \'restored\') = ("restored_at" IS NOT NULL)', name="restored_at_iff_restored"
    ),
    sa.Index("ix_export_upgrade_item", "export_item_id"),
    sa.Index("ix_export_upgrade_created_at", "created_at"),
)

# A volume assembled from chapters (M4 L10, EF-EXP-019, EF-BIB-044): one row per assembly, the
# volume that replaced the chapter files of one destination and what it replaced. `old_items`
# lists `{export_item_id, unit_id, file_hash, trash_item_id}` in reading order: the chapter files
# are in the bin of their root, never deleted, and their export items (with the positions written
# on them) are kept. `derived_hash` is the hash of what the volume was made of, never the hash of
# a content that was received: the volume is no copy of its chapters.
export_assembly = sa.Table(
    "export_assembly",
    metadata,
    ulid_column("id", primary_key=True),
    ulid_column("intent_id"),  # the `assemble` intent; no key, the journal is pruned
    sa.Column(
        "shelf_destination_id",
        sa.String(26),
        sa.ForeignKey("shelf_destination.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "export_item_id",
        sa.String(26),
        sa.ForeignKey("export_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("volume", sa.String(), nullable=False),
    sa.Column("old_items", sa.JSON(), nullable=False),
    sa.Column("derived_hash", sa.String(), nullable=False),
    sa.Column("file_hash", sa.String(), nullable=False),
    now_column(),
    ulid_check("id"),
    ulid_check("intent_id"),
    json_shape_check("old_items", "array"),
    prefixed_hash_check("derived_hash"),
    prefixed_hash_check("file_hash"),
    sa.UniqueConstraint("intent_id", name="uq_export_assembly_intent"),
    sa.UniqueConstraint("export_item_id", name="uq_export_assembly_item"),
    sa.Index("ix_export_assembly_destination", "shelf_destination_id"),
)
