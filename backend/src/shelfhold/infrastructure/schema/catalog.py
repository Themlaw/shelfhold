"""Catalog tables: works, editions, units, variants, contributors (02 section 3.1, ADR-0021).

Conventions shared with the other schema modules: ULID primary keys for every exposed entity,
`''` as sentinel (never NULL) for every part of a unique key, BCP 47 tags in `lang` with the
value as received in `lang_raw` (ENF-I18N-005), partial dates with their precision (EF-BIB-047),
`*_norm_by` = `normalizer_id@version` (ENF-OPS-021).
"""

import sqlalchemy as sa
from sqlalchemy.sql.schema import SchemaItem

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    add_trigger,
    bool_column,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)

READING_ORDERS = ("publication", "chronological", "custom")
READING_DIRECTIONS = ("ltr", "rtl", "vertical")
MEDIA = ("images", "text", "file", "audio")
WORK_STATUSES = ("unknown", "ongoing", "completed", "hiatus", "cancelled")
DATE_PRECISIONS = ("year", "month", "day")
TITLE_KINDS = ("alt", "localized", "romanized")
CONTRIBUTOR_KINDS = ("person", "org")
NAME_KINDS = ("display", "native", "romanized", "sort")
CONTRIBUTION_ENTITIES = ("series", "work", "edition", "volume")
EXTERNAL_ID_ENTITIES = ("series", "work", "edition", "volume", "contributor")
EXTERNAL_ID_ORIGINS = ("source", "provider", "manual", "legacy")
EXTERNAL_ID_STATES = ("current", "superseded")
TRANSLATION_TYPES = ("official", "fan", "machine", "raw")
VOLUME_POLICIES = ("closed_only", "rolling_tail", "whole")
INTEGRITY_STATES = ("unchecked", "ok", "corrupt")
MERGE_KINDS = ("merge", "split")
UNIT_TRACKS = ("main", "annual", "special", "extra", "oneshot", "unnumbered")
UNIT_KINDS = ("chapter", "episode", "book")
UNIT_AVAILABILITIES = ("free", "purchased", "expiring", "locked", "stubbed", "removed")
UPSTREAM_STATUSES = ("ongoing", "finished", "paused", "unknown")
METADATA_ENTITIES = ("series", "work", "edition", "volume", "contributor")
METADATA_PROVENANCES = ("provider", "source", "embedded", "import", "manual")
UNIT_STATES = (
    "known", "wanted", "queued", "stored", "exported", "adopted", "missing", "store_missing",
    "corrupt", "source_removed", "quarantined",
)  # fmt: skip
MERGE_ENTITIES = ("work", "contributor", "variant")
SUGGESTION_STATES = ("open", "accepted", "rejected")
AGE_SUGGESTION_ENTITIES = ("work", "edition")
AGE_PROVENANCES = ("comicinfo", "opf", "sub_source", "provider", "import", "attach")

# Wildcards are not languages: they never enter an Edition's key (ENF-I18N-005).
LANG_WILDCARDS = ("all", "multi", "und", "mul")


def lang_check(column: str, *, allow_wildcards: bool) -> sa.CheckConstraint:
    """A syntactically plausible BCP 47 tag; canonical casing is the normalizer's job."""
    c = f'"{column}"'
    text = (
        f"length({c}) > 0 AND {c} NOT GLOB '*[^A-Za-z0-9-]*' "
        f"AND {c} NOT GLOB '-*' AND {c} NOT GLOB '*-'"
    )
    if not allow_wildcards:
        listing = ", ".join(f"'{w}'" for w in LANG_WILDCARDS)
        text += f" AND lower({c}) NOT IN ({listing})"
    return sa.CheckConstraint(text, name=column)


def norm_by_check(column: str) -> sa.CheckConstraint:
    """`normalizer_id@version`: a normalized value always says which normalizer produced it."""
    c = f'"{column}"'
    return sa.CheckConstraint(
        f"{c} IS NULL OR ({c} GLOB '?*@[0-9]*' AND {c} NOT GLOB '*@*[^0-9]*' "
        f"AND {c} NOT GLOB '*@*@*')",
        name=f"{column}_versioned",
    )


def partial_date_check(date: str, precision: str) -> sa.CheckConstraint:
    """An ISO 8601 date no finer than what was known; no precision is ever invented."""
    d, p = f'"{date}"', f'"{precision}"'
    # `coalesce`: a CHECK that evaluates to NULL passes, which would let `(NULL, 'year')` in
    return sa.CheckConstraint(
        f"coalesce(({d} IS NULL AND {p} IS NULL) "
        f"OR ({p} = 'year' AND length({d}) = 4 AND {d} NOT GLOB '*[^0-9]*') "
        f"OR ({p} = 'month' AND length({d}) = 7 AND date({d} || '-01') = {d} || '-01') "
        f"OR ({p} = 'day' AND length({d}) = 10 AND date({d}) = {d}), 0)",
        name=f"{precision}_coherent",
    )


def age_columns() -> list[SchemaItem]:
    """Minimal age as an integer (not rated = NULL), the X flag, and when it was rated.

    The scale is ordinal and open: a new tier (21, say) needs no migration (EF-EXP-011, I48).
    """
    return [
        sa.Column("age_rating", sa.Integer(), nullable=True),
        bool_column("age_x", default=False),  # stricter than any age
        sa.Column("rated_at", UtcDateTime(), nullable=True),  # last rating, audited
        sa.CheckConstraint(
            "age_rating IS NULL OR (typeof(\"age_rating\") = 'integer' "
            'AND "age_rating" BETWEEN 0 AND 99)',
            name="age_rating_min_age",
        ),
    ]


def _fk(
    column: str, target: str, *, ondelete: str | None = None, nullable: bool = False
) -> sa.Column[str]:
    return sa.Column(
        column, sa.String(26), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


work_kind = sa.Table(
    "work_kind",
    metadata,
    sa.Column("key", sa.String(), primary_key=True),
    sa.Column("label_fr", sa.String(), nullable=False),
    sa.Column("default_shelf", sa.String(), nullable=True),
    sa.Column("default_direction", sa.String(), nullable=True),
    sa.Column("default_media", sa.String(), nullable=False, server_default="images"),
    sa.CheckConstraint("\"key\" <> '' AND \"key\" NOT GLOB '*[^a-z0-9_]*'", name="key_slug"),
    in_check("default_direction", READING_DIRECTIONS),
    in_check("default_media", MEDIA),
)

series = sa.Table(
    "series",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("title", sa.String(), nullable=False),
    sa.Column("sort_title", sa.String(), nullable=False),
    sa.Column("reading_order", sa.String(), nullable=False, server_default="publication"),
    *age_columns(),
    sa.Column("issn", sa.String(), nullable=True),  # prepublication magazine
    now_column(),
    ulid_check("id"),
    in_check("reading_order", READING_ORDERS),
)

work = sa.Table(
    "work",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("kind", sa.String(), sa.ForeignKey("work_kind.key"), nullable=False),
    sa.Column("title", sa.String(), nullable=False),
    sa.Column("sort_title", sa.String(), nullable=False),
    sa.Column("description", sa.Text(), nullable=True),
    sa.Column("status", sa.String(), nullable=False, server_default="unknown"),
    sa.Column("first_published", sa.String(), nullable=True),
    sa.Column("date_precision", sa.String(), nullable=True),
    *age_columns(),
    bool_column("is_adult", default=False),
    sa.Column("reading_direction", sa.String(), nullable=True),  # null: the kind's default
    sa.Column("locked_fields", sa.JSON(), nullable=False, server_default="[]"),
    # The sequence of the `event` table when the Work was created (a trigger sets it and moves the
    # sequence by one, so that every token issued before has a lower value): `?since=` never reports
    # a removal of a Work created after the token (M3 L11). 0: older than any token.
    sa.Column("created_seq", sa.Integer(), nullable=False, server_default="0"),
    now_column(),
    ulid_check("id"),
    in_check("status", WORK_STATUSES),
    in_check("date_precision", DATE_PRECISIONS),
    partial_date_check("first_published", "date_precision"),
    in_check("reading_direction", READING_DIRECTIONS),
    sa.Index("ix_work_kind", "kind"),
)

add_trigger(
    work,
    "work_created_seq",
    "AFTER INSERT ON work BEGIN "
    "INSERT INTO sqlite_sequence (name, seq) SELECT 'event', 0 WHERE NOT EXISTS "
    "(SELECT 1 FROM sqlite_sequence WHERE name = 'event'); "
    "UPDATE sqlite_sequence SET seq = seq + 1 WHERE name = 'event'; "
    "UPDATE work SET created_seq = (SELECT seq FROM sqlite_sequence WHERE name = 'event') "
    "WHERE id = NEW.id; END",
)

series_member = sa.Table(
    "series_member",
    metadata,
    _fk("series_id", "series.id", ondelete="CASCADE"),
    _fk("work_id", "work.id", ondelete="CASCADE"),
    sa.Column("position", sa.String(), nullable=False),  # decimal text: "1", "1.5", "10"
    sa.Column("label", sa.String(), nullable=True),
    sa.PrimaryKeyConstraint("series_id", "work_id"),
    sa.CheckConstraint(
        "\"position\" GLOB '[0-9]*' AND \"position\" NOT GLOB '*[^0-9.]*' "
        "AND \"position\" NOT GLOB '*.*.*' AND \"position\" NOT GLOB '*.'",
        name="position_decimal",
    ),
    sa.Index("ix_series_member_work", "work_id"),
)

work_title = sa.Table(
    "work_title",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    _fk("work_id", "work.id", ondelete="CASCADE"),
    sa.Column("title", sa.String(), nullable=False),
    sa.Column("lang", sa.String(), nullable=False, server_default="und"),  # `und`: not known
    sa.Column("lang_raw", sa.String(), nullable=True),
    sa.Column("kind", sa.String(), nullable=False, server_default="alt"),
    sa.Column("script", sa.String(), nullable=True),  # ISO 15924
    lang_check("lang", allow_wildcards=True),
    in_check("kind", TITLE_KINDS),
    sa.UniqueConstraint("work_id", "lang", "kind", "title"),
)

contributor = sa.Table(
    "contributor",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("kind", sa.String(), nullable=False, server_default="person"),
    sa.Column("primary_name", sa.String(), nullable=False),
    sa.Column("birth_date", sa.String(), nullable=True),
    sa.Column("birth_precision", sa.String(), nullable=True),
    sa.Column("death_date", sa.String(), nullable=True),
    sa.Column("death_precision", sa.String(), nullable=True),
    # "mort pour la France" (EF-SRC-024): NULL = unknown, which the rights review reads cautiously
    sa.Column("died_for_france", sa.Boolean(), nullable=True),
    now_column(),
    ulid_check("id"),
    in_check("kind", CONTRIBUTOR_KINDS),
    in_check("birth_precision", DATE_PRECISIONS),
    in_check("death_precision", DATE_PRECISIONS),
    partial_date_check("birth_date", "birth_precision"),
    partial_date_check("death_date", "death_precision"),
)

contributor_name = sa.Table(
    "contributor_name",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    _fk("contributor_id", "contributor.id", ondelete="CASCADE"),
    sa.Column("name", sa.String(), nullable=False),
    sa.Column("name_raw", sa.String(), nullable=True),
    sa.Column("name_norm_by", sa.String(), nullable=True),
    sa.Column("lang", sa.String(), nullable=False, server_default="und"),
    sa.Column("script", sa.String(), nullable=True),
    sa.Column("kind", sa.String(), nullable=False, server_default="display"),
    bool_column("family_first", default=False),
    lang_check("lang", allow_wildcards=True),
    in_check("kind", NAME_KINDS),
    norm_by_check("name_norm_by"),
    sa.UniqueConstraint("contributor_id", "kind", "lang", "name"),
)

# Polymorphic by design (EF-BIB-003): `entity_id` has no foreign key, the type says where to look.
contribution = sa.Table(
    "contribution",
    metadata,
    _fk("contributor_id", "contributor.id", ondelete="CASCADE"),
    sa.Column("entity_type", sa.String(), nullable=False),
    sa.Column("entity_id", sa.String(26), nullable=False),
    sa.Column("role", sa.String(), nullable=False),  # MARC relator code
    sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
    sa.PrimaryKeyConstraint("contributor_id", "entity_type", "entity_id", "role"),
    ulid_check("entity_id"),
    in_check("entity_type", CONTRIBUTION_ENTITIES),
    sa.CheckConstraint(
        'length("role") = 3 AND "role" NOT GLOB \'*[^a-z]*\'', name="role_marc_relator"
    ),
    positive_check("position"),
    sa.Index("ix_contribution_entity", "entity_type", "entity_id"),
)

# "Add, never overwrite": a renumbering adds the new value and marks the old one superseded.
external_id = sa.Table(
    "external_id",
    metadata,
    sa.Column("entity_type", sa.String(), nullable=False),
    sa.Column("entity_id", sa.String(26), nullable=False),
    sa.Column("provider", sa.String(), nullable=False),
    sa.Column("value", sa.String(), nullable=False),
    sa.Column("origin", sa.String(), nullable=False),
    sa.Column("id_scheme", sa.String(), nullable=True),
    sa.Column("state", sa.String(), nullable=False, server_default="current"),
    now_column(),
    sa.PrimaryKeyConstraint("entity_type", "provider", "value"),
    ulid_check("entity_id"),
    in_check("entity_type", EXTERNAL_ID_ENTITIES),
    in_check("origin", EXTERNAL_ID_ORIGINS),
    in_check("state", EXTERNAL_ID_STATES),
    sa.CheckConstraint(
        "\"provider\" <> '' AND \"provider\" NOT GLOB '*[^a-z0-9_]*'", name="provider_key"
    ),
    sa.CheckConstraint("\"value\" <> ''", name="value_not_empty"),
    sa.Index("ix_external_id_entity", "entity_type", "entity_id"),
)
# Append-only in the database (L12): the one change allowed is the mark of a renumbering, once
# (`current` -> `superseded`, the origin kept or set to `legacy`); nothing else is rewritten.
add_trigger(
    external_id,
    "external_id_no_update",
    "BEFORE UPDATE ON external_id "
    "WHEN OLD.entity_type IS NOT NEW.entity_type OR OLD.entity_id IS NOT NEW.entity_id "
    "OR OLD.provider IS NOT NEW.provider OR OLD.value IS NOT NEW.value "
    "OR OLD.id_scheme IS NOT NEW.id_scheme OR OLD.created_at IS NOT NEW.created_at "
    "OR OLD.state IS NOT 'current' OR NEW.state IS NOT 'superseded' "
    "OR (NEW.origin IS NOT OLD.origin AND NEW.origin IS NOT 'legacy') "
    "BEGIN SELECT RAISE(ABORT, 'external_id is append-only'); END",
)
add_trigger(
    external_id,
    "external_id_no_delete",
    "BEFORE DELETE ON external_id BEGIN SELECT RAISE(ABORT, 'external_id is append-only'); END",
)
# `INSERT OR REPLACE` resolves a conflict by deleting the old row without a DELETE trigger.
add_trigger(
    external_id,
    "external_id_no_replace",
    "BEFORE INSERT ON external_id "
    "WHEN EXISTS (SELECT 1 FROM external_id WHERE entity_type = NEW.entity_type "
    "AND provider = NEW.provider AND value = NEW.value) "
    "BEGIN SELECT RAISE(ABORT, 'external_id is append-only'); END",
)

# Works related without being merged (EF-BIB-039): a hook of V1, open vocabulary like work_kind.
work_relation_kind = sa.Table(
    "work_relation_kind",
    metadata,
    sa.Column("key", sa.String(), primary_key=True),
    sa.Column("label_fr", sa.String(), nullable=False),
    sa.CheckConstraint("\"key\" <> '' AND \"key\" NOT GLOB '*[^a-z0-9_]*'", name="key_slug"),
)

# Journal of merges and splits of Works, undoable for 30 days (EF-BIB-019). The identifiers are not
# foreign keys: the absorbed Work is gone once the merge is done, the journal must outlive it.
merge_log = sa.Table(
    "merge_log",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("kind", sa.String(), nullable=False),
    ulid_column("survivor_id"),
    ulid_column("absorbed_id"),
    sa.Column("snapshot", sa.JSON(), nullable=False),  # what is needed to restore the two Works
    now_column(),
    sa.Column("undone_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    ulid_check("survivor_id"),
    ulid_check("absorbed_id"),
    in_check("kind", MERGE_KINDS),
    json_shape_check("snapshot", "object"),
    sa.CheckConstraint('"survivor_id" <> "absorbed_id"', name="distinct_works"),
    sa.Index("ix_merge_log_survivor", "survivor_id"),
    sa.Index("ix_merge_log_absorbed", "absorbed_id"),
)

# An old Work identifier that now leads to another one (EF-BIB-019): links keep working.
work_redirect = sa.Table(
    "work_redirect",
    metadata,
    ulid_column("from_work_id", primary_key=True),
    sa.Column(
        "to_work_id", sa.String(26), sa.ForeignKey("work.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("merge_log_id", sa.String(26), sa.ForeignKey("merge_log.id", ondelete="SET NULL")),
    now_column(),
    ulid_check("from_work_id"),
    sa.CheckConstraint('"from_work_id" <> "to_work_id"', name="no_self_redirect"),
    sa.Index("ix_work_redirect_target", "to_work_id"),
)

work_relation = sa.Table(
    "work_relation",
    metadata,
    _fk("from_work_id", "work.id", ondelete="CASCADE"),
    _fk("to_work_id", "work.id", ondelete="CASCADE"),
    sa.Column("kind", sa.String(), sa.ForeignKey("work_relation_kind.key"), nullable=False),
    sa.Column("origin", sa.String(), nullable=False),
    now_column(),
    sa.PrimaryKeyConstraint("from_work_id", "to_work_id", "kind"),
    sa.CheckConstraint('"from_work_id" <> "to_work_id"', name="distinct_works"),
    sa.CheckConstraint("\"origin\" <> ''", name="origin_not_empty"),
    sa.Index("ix_work_relation_to", "to_work_id"),
)

# The sextuplet of ADR-0021 plus `performance` (audio readings, K6) and `packaging` (reissues,
# GAP-8-03). Every part of the key is NOT NULL with '' as sentinel: SQLite treats NULLs as
# distinct in a unique index, which would let duplicates in.
edition = sa.Table(
    "edition",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("work_id", "work.id"),
    sa.Column("lang", sa.String(), nullable=False),
    sa.Column("lang_raw", sa.String(), nullable=True),
    sa.Column(
        "lang_norm_by", sa.String(), nullable=True
    ),  # `lang@N`: part of the key (ENF-OPS-021)
    sa.Column("translation_type", sa.String(), nullable=False),
    sa.Column("media", sa.String(), nullable=False),
    sa.Column("publisher", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("text_version", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("performance", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("packaging", sa.String(), nullable=False, server_default=sa.text("''")),
    # text_version is derived from a reference, never from a name string (EF-BIB-049)
    _fk("text_version_contributor_id", "contributor.id", nullable=True),
    sa.Column("text_version_label", sa.String(), nullable=True),
    sa.Column("text_version_norm_by", sa.String(), nullable=True),
    _fk("translated_from_edition_id", "edition.id", nullable=True),  # relay translation (U6)
    sa.Column("published_on", sa.String(), nullable=True),
    sa.Column("date_precision", sa.String(), nullable=True),
    bool_column("is_official", default=False),
    bool_column("preferred", default=False),
    *age_columns(),
    sa.Column("rights", sa.Text(), nullable=True),  # SPDX or text + origin URL
    sa.Column("rights_basis", sa.String(), nullable=True),
    sa.Column("rights_jurisdiction", sa.String(), nullable=True),
    sa.Column("author_death_year", sa.Integer(), nullable=True),  # derived cache (F11)
    sa.Column("translator", sa.String(), nullable=True),  # derived cache (F11)
    sa.Column("translator_death_year", sa.Integer(), nullable=True),
    sa.Column("volume_policy", sa.String(), nullable=False, server_default="closed_only"),
    # Chapters per volume when the source declares no volumes (D5, EF-BIB-024); per Edition.
    sa.Column("volume_size", sa.Integer(), nullable=False, server_default="50"),
    # What the upstream says of the series (ongoing, finished...), and who said it (EF-SRC-038).
    sa.Column("upstream_status", sa.String(), nullable=False, server_default="unknown"),
    sa.Column("upstream_status_by", sa.String(), nullable=True),
    sa.Column("upstream_status_at", UtcDateTime(), nullable=True),
    # The shelf this Edition is filed on; NULL = unshelved ("À ranger", never exported). The
    # shelf table arrives in 0004; deleting a shelf leaves its Editions unshelved (GAP-12-11).
    _fk("shelf_id", "shelf.id", ondelete="SET NULL", nullable=True),
    now_column(),
    ulid_check("id"),
    lang_check("lang", allow_wildcards=False),
    in_check("translation_type", TRANSLATION_TYPES),
    in_check("media", MEDIA),
    in_check("date_precision", DATE_PRECISIONS),
    partial_date_check("published_on", "date_precision"),
    in_check("volume_policy", VOLUME_POLICIES),
    positive_check("volume_size", minimum=1),
    in_check("upstream_status", UPSTREAM_STATUSES),
    in_check("upstream_status_by", METADATA_PROVENANCES),
    norm_by_check("text_version_norm_by"),
    norm_by_check("lang_norm_by"),
    sa.CheckConstraint(
        '("text_version" = \'\' AND "text_version_contributor_id" IS NULL '
        'AND "text_version_label" IS NULL) '
        'OR ("text_version" <> \'\' AND ("text_version_contributor_id" IS NOT NULL '
        'OR "text_version_label" IS NOT NULL))',
        name="text_version_has_reference",
    ),
    sa.CheckConstraint(
        "typeof(\"author_death_year\") IN ('integer', 'null') "
        "AND typeof(\"translator_death_year\") IN ('integer', 'null')",
        name="death_year_integer",
    ),
    sa.UniqueConstraint(
        "work_id",
        "lang",
        "translation_type",
        "media",
        "publisher",
        "text_version",
        "performance",
        "packaging",
        name="uq_edition_key",
    ),
    # target of the composite foreign keys that tie a binding or a follow to its own work
    sa.UniqueConstraint("id", "work_id", name="uq_edition_id_work_id"),
    sa.Index("ix_edition_text_version_contributor", "text_version_contributor_id"),
    sa.Index("ix_edition_translated_from", "translated_from_edition_id"),
    sa.Index("ix_edition_shelf", "shelf_id"),
)

volume = sa.Table(
    "volume",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("edition_id", "edition.id"),
    sa.Column("number", sa.String(), nullable=False),
    sa.Column("number_sort", sa.Float(), nullable=True),
    sa.Column("title", sa.String(), nullable=True),
    sa.Column("isbn", sa.String(), nullable=True),  # ISBN-13, digits only
    sa.Column("cover_ref", sa.String(), nullable=True),
    bool_column("closed", default=False),  # announced closed by the source or frozen by policy
    now_column(),
    ulid_check("id"),
    sa.CheckConstraint(
        '"isbn" IS NULL OR (length("isbn") = 13 AND "isbn" NOT GLOB \'*[^0-9]*\')',
        name="isbn_13_digits",
    ),
    sa.UniqueConstraint("edition_id", "number"),
)

# The logical Unit is a place in an Edition: (edition, track, segment, number, part). Which file
# fills it is a Variant (below). `number_norm` is the normalizer's output; `number_raw` is kept.
unit = sa.Table(
    "unit",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("edition_id", "edition.id"),
    _fk("volume_id", "volume.id", ondelete="SET NULL", nullable=True),
    sa.Column("number_raw", sa.String(), nullable=False),
    sa.Column("number_norm", sa.String(), nullable=False),
    sa.Column("number_norm_by", sa.String(), nullable=True),
    sa.Column("number_sort", sa.Float(), nullable=True),
    sa.Column("number_segment", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("number_segment_norm_by", sa.String(), nullable=True),
    sa.Column("number_segment_ordinal", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("part", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("track", sa.String(), nullable=False, server_default="main"),
    sa.Column("source_order", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("kind", sa.String(), nullable=False, server_default="chapter"),
    sa.Column("title", sa.String(), nullable=True),
    sa.Column("published_on", sa.String(), nullable=True),
    sa.Column("date_precision", sa.String(), nullable=True),
    sa.Column("published_at", UtcDateTime(), nullable=True),  # upload time, informative
    sa.Column("availability", sa.String(), nullable=False, server_default="free"),
    # A value the SDK contract does not know (D20): stored here, treated as `locked`.
    sa.Column("availability_detail", sa.String(), nullable=True),
    # Why a follow filter left the Unit out of the download queue (EF-SRC-039); open vocabulary.
    sa.Column("filtered_reason", sa.String(), nullable=True),
    sa.Column("available_until", UtcDateTime(), nullable=True),
    sa.Column("state", sa.String(), nullable=False, server_default="known"),
    bool_column("follow_suppressed", default=False),  # "stop following", never lifted by itself
    sa.Column("follow_suppressed_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    sa.Column("follow_suppressed_at", UtcDateTime(), nullable=True),
    now_column(),
    # Integrity check of the Unit's file (EF-BIB-012); NULL until the first check.
    sa.Column("integrity_state", sa.String(), nullable=False, server_default="unchecked"),
    sa.Column("integrity_checked_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    in_check("integrity_state", INTEGRITY_STATES),
    in_check("track", UNIT_TRACKS),
    in_check("kind", UNIT_KINDS),
    in_check("availability", UNIT_AVAILABILITIES),
    in_check("state", UNIT_STATES),
    in_check("date_precision", DATE_PRECISIONS),
    partial_date_check("published_on", "date_precision"),
    norm_by_check("number_norm_by"),
    norm_by_check("number_segment_norm_by"),
    sa.CheckConstraint(
        "\"track\" = 'unnumbered' OR \"number_norm\" <> ''", name="numbered_track_has_number"
    ),
    positive_check("number_segment_ordinal"),
    positive_check("source_order"),
    sa.Index("ix_unit_volume", "volume_id"),
    sa.Index("ix_unit_state", "edition_id", "state"),
)
# The place (EF-BIB-034, EF-BIB-050). An extra without a number has no `number_norm`: its place
# is its order in the source, which is what the second index says.
sa.Index(
    "uq_unit_place",
    unit.c.edition_id,
    unit.c.track,
    unit.c.number_segment,
    unit.c.number_norm,
    unit.c.part,
    unique=True,
    sqlite_where=sa.text("\"track\" <> 'unnumbered'"),
)
sa.Index(
    "uq_unit_unnumbered_order",
    unit.c.edition_id,
    unit.c.source_order,
    unique=True,
    sqlite_where=sa.text("\"track\" = 'unnumbered'"),
)

# A translation team (scanlation) belongs to an Edition, never to a binding (G3, EF-BIB-035).
team = sa.Table(
    "team",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("edition_id", "edition.id"),
    sa.Column("label", sa.String(), nullable=False),  # normalized
    sa.Column("label_raw", sa.String(), nullable=True),
    sa.Column("label_norm_by", sa.String(), nullable=True),
    sa.Column("last_release_at", UtcDateTime(), nullable=True),
    now_column(),
    # Numbering offset between teams (EF-BIB-028, may be cut): laid down, no behaviour yet.
    sa.Column("number_offset", sa.Integer(), nullable=False, server_default="0"),
    ulid_check("id"),
    sa.CheckConstraint("typeof(\"number_offset\") = 'integer'", name="number_offset_integer"),
    norm_by_check("label_norm_by"),
    sa.UniqueConstraint("edition_id", "label"),
    # target of the composite foreign key that keeps a team_priority within its own Edition
    sa.UniqueConstraint("id", "edition_id"),
)

team_alias = sa.Table(
    "team_alias",
    metadata,
    _fk("team_id", "team.id", ondelete="CASCADE"),
    _fk("source_binding_id", "source_binding.id", ondelete="CASCADE"),
    sa.Column("label", sa.String(), nullable=False),  # as seen on that binding
    sa.Column("first_seen_at", UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint("team_id", "source_binding_id", "label"),
    sa.Index("ix_team_alias_binding", "source_binding_id"),
)

team_priority = sa.Table(
    "team_priority",
    metadata,
    _fk("edition_id", "edition.id", ondelete="CASCADE"),
    sa.Column("team_id", sa.String(26), nullable=False),
    sa.Column("rank", sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(
        ["team_id", "edition_id"], ["team.id", "team.edition_id"], ondelete="CASCADE"
    ),
    sa.PrimaryKeyConstraint("edition_id", "team_id"),
    sa.UniqueConstraint("edition_id", "rank"),
    positive_check("rank", minimum=1),
)

# A Variant is what one source, and one team, gives for a place. Identity: the binding plus the
# upstream identifier, which is never rewritten by a revision (EF-BIB-050): a new identifier is
# a new Variant, linked to the one it replaces by `supersedes_variant_id`.
unit_variant = sa.Table(
    "unit_variant",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("unit_id", "unit.id"),
    _fk("source_binding_id", "source_binding.id"),
    sa.Column("remote_unit_id", sa.String(), nullable=False),
    sa.Column("remote_unit_id_norm_by", sa.String(), nullable=True),  # `remote_unit_id@N`
    _fk("source_item_id", "source_item.id", nullable=True),
    _fk("team_id", "team.id", ondelete="SET NULL", nullable=True),
    sa.Column("lang", sa.String(), nullable=False, server_default="und"),
    sa.Column("lang_raw", sa.String(), nullable=True),
    sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("revision_hint", sa.String(), nullable=True),
    _fk("supersedes_variant_id", "unit_variant.id", ondelete="SET NULL", nullable=True),
    sa.Column("fetch_handle", sa.String(), nullable=True),  # local integer of the upstream, cache
    sa.Column("fetch_handle_seen_at", UtcDateTime(), nullable=True),
    sa.Column("quality_score", sa.Float(), nullable=True),
    bool_column("preferred", default=False),
    sa.Column(
        "first_seen_at", UtcDateTime(), nullable=False, server_default=sa.func.current_timestamp()
    ),
    sa.Column("removed_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    lang_check("lang", allow_wildcards=True),
    positive_check("revision", minimum=1),
    sa.CheckConstraint("\"remote_unit_id\" <> ''", name="remote_unit_id_not_empty"),
    norm_by_check("remote_unit_id_norm_by"),
    sa.UniqueConstraint("source_binding_id", "remote_unit_id"),
    # target of the composite foreign key that keeps a content within its Variant's own Unit
    sa.UniqueConstraint("id", "unit_id"),
    sa.Index("ix_unit_variant_unit", "unit_id"),
    sa.Index("ix_unit_variant_team", "team_id"),
    sa.Index("ix_unit_variant_supersedes", "supersedes_variant_id"),
)

# At most one preferred Variant per Unit (the one exported, EF-BIB-028).
sa.Index(
    "uq_unit_variant_preferred",
    unit_variant.c.unit_id,
    unique=True,
    sqlite_where=sa.text('"preferred"'),
)

# Append-only history of a Variant's revisions (H2). Read by the suspect-revision guard (M2)
# and by `revision_pending`. Erasing a Variant takes its history with it (EF-BIB-052).
variant_revision = sa.Table(
    "variant_revision",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    _fk("variant_id", "unit_variant.id", ondelete="CASCADE"),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("seen_at", UtcDateTime(), nullable=False),
    sa.Column("page_count", sa.Integer(), nullable=True),
    sa.Column("content_hash", sa.String(), nullable=True),
    sa.Column("revision_hint", sa.String(), nullable=True),
    # Set once by the suspect-revision guard, never lifted: the Variant row is not rebuilt.
    sa.Column("quarantined_at", UtcDateTime(), nullable=True),
    sa.Column("quarantine_reason", sa.String(), nullable=True),
    prefixed_hash_check("content_hash"),
    positive_check("revision", minimum=1),
    positive_check("page_count"),
    sa.UniqueConstraint("variant_id", "revision"),
)
# Only the quarantine mark may be written, once: every other column, and a mark already set,
# stay as they were.
add_trigger(
    variant_revision,
    "variant_revision_no_update",
    "BEFORE UPDATE ON variant_revision "
    "WHEN OLD.id IS NOT NEW.id OR OLD.variant_id IS NOT NEW.variant_id "
    "OR OLD.revision IS NOT NEW.revision OR OLD.seen_at IS NOT NEW.seen_at "
    "OR OLD.page_count IS NOT NEW.page_count OR OLD.content_hash IS NOT NEW.content_hash "
    "OR OLD.revision_hint IS NOT NEW.revision_hint OR OLD.quarantined_at IS NOT NULL "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END",
)
# `INSERT OR REPLACE` resolves a conflict by deleting the old row, and SQLite fires no DELETE
# trigger for that unless `recursive_triggers` is on: refuse the colliding insert itself.
add_trigger(
    variant_revision,
    "variant_revision_no_replace",
    "BEFORE INSERT ON variant_revision "
    "WHEN EXISTS (SELECT 1 FROM variant_revision "
    "WHERE variant_id = NEW.variant_id AND revision = NEW.revision) "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END",
)
# A direct delete is refused while the Variant lives; the cascade of an erasure runs once the
# Variant row is gone, so the guard lets it through.
add_trigger(
    variant_revision,
    "variant_revision_no_delete",
    "BEFORE DELETE ON variant_revision "
    "WHEN EXISTS (SELECT 1 FROM unit_variant WHERE id = OLD.variant_id) "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END",
)

merge_suggestion = sa.Table(
    "merge_suggestion",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("entity_type", sa.String(), nullable=False),
    sa.Column("a", sa.String(26), nullable=False),
    sa.Column("b", sa.String(26), nullable=False),
    sa.Column("score", sa.Float(), nullable=True),
    sa.Column("reason", sa.String(), nullable=False),  # open vocabulary: numbering_conflict, ...
    sa.Column("state", sa.String(), nullable=False, server_default="open"),
    now_column(),
    sa.Column("decided_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    ulid_check("a"),
    ulid_check("b"),
    in_check("entity_type", MERGE_ENTITIES),
    in_check("state", SUGGESTION_STATES),
    sa.CheckConstraint('"a" < "b"', name="pair_ordered"),  # one row per unordered pair
    sa.CheckConstraint("\"reason\" <> ''", name="reason_not_empty"),
    sa.UniqueConstraint("entity_type", "a", "b", "reason"),
)

# Ages seen in files, sources and providers. Never a rating (EF-BIB-036).
age_suggestion = sa.Table(
    "age_suggestion",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("entity_type", sa.String(), nullable=False),
    sa.Column("entity_id", sa.String(26), nullable=False),
    sa.Column("value", sa.Integer(), nullable=False),
    bool_column("x", default=False),
    sa.Column("provenance", sa.String(), nullable=False),
    sa.Column("seen_at", UtcDateTime(), nullable=False),
    sa.Column("state", sa.String(), nullable=False, server_default="open"),
    ulid_check("id"),
    ulid_check("entity_id"),
    in_check("entity_type", AGE_SUGGESTION_ENTITIES),
    in_check("provenance", AGE_PROVENANCES),
    in_check("state", SUGGESTION_STATES),
    sa.CheckConstraint('"value" BETWEEN 0 AND 99', name="value_min_age"),
    sa.UniqueConstraint("entity_type", "entity_id", "value", "x", "provenance"),
)

# Registry of versioned normalizers (ENF-OPS-021). A version cited by a tombstone is never
# removed from the code; `cited_by_tombstone` is computed, not stored.
normalizer = sa.Table(
    "normalizer",
    metadata,
    sa.Column("id", sa.String(), nullable=False),
    sa.Column("version", sa.Integer(), nullable=False),
    sa.Column("introduced_in", sa.String(), nullable=False),  # release of Shelfhold
    sa.PrimaryKeyConstraint("id", "version"),
    positive_check("version", minimum=1),
    sa.CheckConstraint("\"id\" <> '' AND \"id\" NOT GLOB '*[^a-z0-9_-]*'", name="id_slug"),
)

# One value of one field of one entity, with where it comes from (EF-BIB-008, EF-SRC-012). The
# key is the unit of override: `locked` keeps a hand-edited value from being refreshed.
metadata_field = sa.Table(
    "metadata_field",
    metadata,
    sa.Column("entity_type", sa.String(), nullable=False),
    sa.Column("entity_id", sa.String(26), nullable=False),  # polymorphic: no foreign key
    sa.Column("field", sa.String(), nullable=False),
    sa.Column("lang", sa.String(), nullable=False, server_default="und"),
    sa.Column("value", sa.Text(), nullable=False),
    sa.Column("provider", sa.String(), nullable=False),
    sa.Column("provenance", sa.String(), nullable=False),
    sa.Column("confidence", sa.Float(), nullable=True),
    bool_column("locked", default=False),
    sa.Column("fetched_at", UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint("entity_type", "entity_id", "field", "lang"),
    ulid_check("entity_id"),
    in_check("entity_type", METADATA_ENTITIES),
    in_check("provenance", METADATA_PROVENANCES),
    lang_check("lang", allow_wildcards=True),
    sa.CheckConstraint("\"field\" <> '' AND \"provider\" <> ''", name="field_provider_named"),
    sa.CheckConstraint(
        '"confidence" IS NULL OR "confidence" BETWEEN 0 AND 1', name="confidence_range"
    ),
    sa.Index("ix_metadata_field_entity", "entity_type", "entity_id"),
)

# The journal of attaches by external identifier (EF-BIB-003, EF-BIB-037, L12): what an attach
# changed, kept so that "Séparer" undoes it exactly, and the suggestions that wait for an admin
# (an attach that would raise the age is never automatic). `prior` holds the state of the Edition
# and of the fields the attach wrote. Who decided is in the audit journal.
EDITION_ATTACH_STATES = ("suggested", "attached", "undone", "declined")
EDITION_ATTACH_REASONS = ("external_id", "age_raise")
edition_attach = sa.Table(
    "edition_attach",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("edition_id", "edition.id", ondelete="CASCADE"),
    _fk("from_work_id", "work.id", ondelete="CASCADE"),
    _fk("to_work_id", "work.id", ondelete="CASCADE"),
    sa.Column("provider", sa.String(), nullable=False),
    sa.Column("value", sa.String(), nullable=False),
    sa.Column("state", sa.String(), nullable=False),
    sa.Column("reason", sa.String(), nullable=False),
    sa.Column("prior", sa.JSON(), nullable=False, server_default="{}"),
    now_column(),
    sa.Column("decided_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    in_check("state", EDITION_ATTACH_STATES),
    in_check("reason", EDITION_ATTACH_REASONS),
    json_shape_check("prior", "object"),
    sa.CheckConstraint('"from_work_id" <> "to_work_id"', name="distinct_works"),
    sa.Index("ix_edition_attach_edition", "edition_id"),
)
