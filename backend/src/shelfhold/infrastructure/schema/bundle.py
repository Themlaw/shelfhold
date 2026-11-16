"""The block of a series served on OPDS (M4 L49a, ADR-0053 §3, §5, §8; ADR-0048).

Three tables, and no file: a block is virtual, its bytes are made at the reading from the files
already exported.

- `bundle_partition`: where a part begins (`first_unit_id`), written when the part is first made
  and never moved, so that a chapter replaced at the start changes only its own part. The
  `export_item` of kind `bundle` it points at is what `document_alias` rows hang on.
- `bundle_version`: one file a profile can download, numbered per (Edition, part, profile). It
  holds the exact size, the keyed fingerprint of the whole (`content_fp`), KOReader's partial MD5
  of the bytes (computed without materialising them), and a `state_key`, a digest of the manifest
  rows it was made from, so the feed can tell whether a version is still current without opening a
  file. A purged version keeps its row (`purged_at`) and loses its members: it answers 410. The
  row has no foreign key to its partition: a boundary that goes (the series was re-cut) purges its
  versions but not their rows, so the number of the next version never goes back (a number is
  never served twice for the same Edition, part and profile).
- `bundle_setting`: the slice of an Edition's block (L49b), a number of chapters a new part may
  hold. A row is a choice of the admin; no row, the cut is the one of the bytes. It moves no
  written boundary.
- `bundle_member`: what a version is made of, in rank order, with the table of pages computed by
  Shelfhold (`first_page`, `page_count`) and a keyed fingerprint of the file (`content_fp`), never
  a clear hash (ADR-0048). When the exported file's row is deleted the member stays as an orphan
  with its fingerprints wiped (a trigger).

`page_map_state` of a member (ADR-0053 §2): `candidate` (Shelfhold's table of pages can be
reproduced by Kavita: canonical names), `unverified` (it cannot, or Kavita counts another number
of pages) and `verified` (Kavita announces the very number of pages of the member). Only a
`verified` member is relayed to Kavita (M4 L50). `kavita_pages` is the count Kavita announced the
last time the two were compared; it is empty for a member that never was (names that Kavita would
not sort alike), which is never compared.
"""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    add_trigger,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)

PAGE_MAP_STATES = ("candidate", "unverified", "verified")
_HEX64 = "length({c}) = 64 AND {c} NOT GLOB '*[^0-9a-f]*'"


def _hex_check(column: str) -> sa.CheckConstraint:
    c = f'"{column}"'
    return sa.CheckConstraint(f"{c} IS NULL OR ({_HEX64.format(c=c)})", name=f"{column}_hex64")


bundle_partition = sa.Table(
    "bundle_partition",
    metadata,
    sa.Column(
        "edition_id", sa.String(26), sa.ForeignKey("edition.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("part_index", sa.Integer(), nullable=False),
    sa.Column("first_unit_id", sa.String(26), nullable=True),  # NULL: the part opens the series
    sa.Column(
        "export_item_id",
        sa.String(26),
        sa.ForeignKey("export_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    now_column(),
    sa.PrimaryKeyConstraint("edition_id", "part_index"),
    ulid_check("first_unit_id"),
    ulid_check("export_item_id"),
    positive_check("part_index", minimum=1),
    sa.UniqueConstraint("export_item_id"),
)

bundle_setting = sa.Table(
    "bundle_setting",
    metadata,
    sa.Column(
        "edition_id", sa.String(26), sa.ForeignKey("edition.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("slice_chapters", sa.Integer(), nullable=False),
    now_column("updated_at"),
    sa.PrimaryKeyConstraint("edition_id"),
    ulid_check("edition_id"),
    sa.CheckConstraint(
        '"slice_chapters" >= 1 AND "slice_chapters" <= 256', name="slice_chapters_range"
    ),
)

bundle_version = sa.Table(
    "bundle_version",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "edition_id", sa.String(26), sa.ForeignKey("edition.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("part_index", sa.Integer(), nullable=False),  # no foreign key: see below
    sa.Column(
        "profile_user_id",
        sa.String(26),
        sa.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("number", sa.Integer(), nullable=False),
    sa.Column("content_fp", sa.String(), nullable=False),  # HMAC of the whole, hex
    sa.Column("koreader_md5", sa.String(), nullable=False),  # `md5:` + hex, of the stream's bytes
    sa.Column("state_key", sa.String(), nullable=False),  # digest of the manifest rows, hex
    sa.Column("size", sa.BigInteger(), nullable=False),
    sa.Column("entry_count", sa.Integer(), nullable=False),
    sa.Column("page_count", sa.Integer(), nullable=False),
    sa.Column("member_count", sa.Integer(), nullable=False),
    sa.Column("excluded", sa.JSON(), nullable=False, server_default="{}"),  # reason -> chapters
    now_column(),
    sa.Column("last_used_at", UtcDateTime(), nullable=True),
    sa.Column("purged_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    positive_check("number", minimum=1),
    _hex_check("content_fp"),
    _hex_check("state_key"),
    prefixed_hash_check("koreader_md5", "md5", 32),
    json_shape_check("excluded", "object"),
    sa.UniqueConstraint("edition_id", "part_index", "profile_user_id", "number"),
    sa.Index("ix_bundle_version_profile", "profile_user_id"),
    sa.Index("ix_bundle_version_part", "edition_id", "part_index", "profile_user_id"),
)

bundle_member = sa.Table(
    "bundle_member",
    metadata,
    sa.Column(
        "version_id",
        sa.String(26),
        sa.ForeignKey("bundle_version.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("rank", sa.Integer(), nullable=False),
    sa.Column(
        "export_item_id", sa.String(26), sa.ForeignKey("export_item.id", ondelete="SET NULL")
    ),
    sa.Column("unit_id", sa.String(26), sa.ForeignKey("unit.id", ondelete="SET NULL")),
    sa.Column("chapter_label", sa.String(), nullable=False),
    sa.Column("content_fp", sa.String(), nullable=True),  # HMAC of the file's hash; NULL: purged
    sa.Column("pages_digest", sa.String(), nullable=True),  # names, sizes, CRC-32 of the pages
    sa.Column("first_page", sa.Integer(), nullable=False),  # counted by Shelfhold, from 1
    sa.Column("page_count", sa.Integer(), nullable=False),
    sa.Column("page_map_state", sa.String(), nullable=False, server_default="candidate"),
    sa.Column("kavita_pages", sa.Integer(), nullable=True),  # what Kavita announced when compared
    sa.PrimaryKeyConstraint("version_id", "rank"),
    in_check("page_map_state", PAGE_MAP_STATES),
    positive_check("kavita_pages"),
    positive_check("rank", minimum=1),
    sa.CheckConstraint('"first_page" >= 1 AND "page_count" >= 0', name="pages_positive"),
    _hex_check("content_fp"),
    _hex_check("pages_digest"),
    sa.Index("ix_bundle_member_item", "export_item_id"),
)

# A purged file row: the version keeps its member as an orphan, with no fingerprint of the file
# (ADR-0048: a purged hash is no longer anywhere in the clear, and a keyed one is not kept either).
add_trigger(
    bundle_member,
    "export_item_wipe_bundle_members",
    "BEFORE DELETE ON export_item BEGIN "
    "UPDATE bundle_member SET content_fp = NULL, pages_digest = NULL "
    "WHERE export_item_id = OLD.id; END",
)
