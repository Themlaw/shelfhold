"""Reading hooks: the raw anchor of a position and the progress projected from it (02 section 3.4).

In the schema from M1a, filled in M4 (Kavita) and M5 (reader, kosync). A position is the
client's own report on the file it was reading; the progress per Unit is recomputed, never
written by a client (EF-LEC-014, ADR-0045).
"""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    add_view,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)

POSITION_CARRIERS = ("export_item", "content")  # `content`: a Work read unexported (AA5)
# `device`: a reader's own report (KOReader, the integrated reader): it never marks a Unit read by
# itself, only a page-by-page traversal does (ADR-0053). `server`: the read state a server keeps
# itself (Kavita), taken as it is.
POSITION_SOURCES = ("device", "server")

# History is kept (offline merge by `client_ts` bounded by `server_ts`); no foreign key on the
# carrier, so a position outlives the file it was read on and is found again by `content_hash`.
reading_position = sa.Table(
    "reading_position",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("carrier_type", sa.String(), nullable=False),
    sa.Column("carrier_id", sa.String(26), nullable=False),
    sa.Column("content_hash", sa.String(), nullable=False),
    sa.Column("locator", sa.JSON(), nullable=False),  # href, cfi, progression, page...
    sa.Column("percent", sa.Float(), nullable=True),
    sa.Column("client_ts", UtcDateTime(), nullable=False),
    now_column("server_ts"),
    sa.Column("device_id", sa.String(), nullable=True),
    # 0065 (L14a). `page`, when there is one, is an integer: a text would sort above every number.
    sa.Column("source", sa.String(), nullable=False, server_default="device"),
    # The Units the carrier covered when the position was written (ordered ids): a position on a
    # replaced content reaches these, never the Units a later cut put there.
    sa.Column("covered_units", sa.JSON(none_as_null=True), nullable=True),
    # The Units this position's traversal marked read (a journal), and, by Unit, when a member
    # undid that mark (`{unit_id: UTC time}`).
    sa.Column("read_units", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("undone_units", sa.JSON(), nullable=False, server_default="{}"),
    ulid_check("id"),
    ulid_check("carrier_id"),
    in_check("source", POSITION_SOURCES),
    sa.CheckConstraint(
        '"covered_units" IS NULL OR (json_valid("covered_units") '
        "AND json_type(\"covered_units\") = 'array')",
        name="covered_units_list",
    ),
    json_shape_check("read_units", "array"),
    json_shape_check("undone_units", "object"),
    sa.CheckConstraint(
        "json_type(\"locator\", '$.page') IS NULL OR json_type(\"locator\", '$.page') = 'integer'",
        name="page_integer",
    ),
    in_check("carrier_type", POSITION_CARRIERS),
    prefixed_hash_check("content_hash"),
    json_shape_check("locator", "object"),
    sa.CheckConstraint('"percent" IS NULL OR "percent" BETWEEN 0 AND 1', name="percent_range"),
    sa.Index("ix_reading_position_user", "user_id", "carrier_type", "carrier_id", "server_ts"),
    sa.Index("ix_reading_position_content_hash", "content_hash"),
)

# What a reader got to in a Unit, merged over every position that reaches it (EF-LEC-014,
# EF-LEC-007). `reading.domain` states the rule and `test_the_view_is_the_domain_rule` proves the
# view equal to it. Columns (frozen since 1.1, `docs/reference/reading-progress.md`): `user_id`,
# `unit_id`, `state`, `last_position_id` (the FURTHEST position), `latest_position_id` (the LATEST
# one written, `client_ts` bounded by `server_ts`), `approximate` (0 or 1).
#   * per position and Unit: exact with an integer page and known bounds, else proportional to the
#     Unit's rank among the Units of the file and `approximate`, except for a file that holds one
#     Unit. A position whose `content_hash` is not the one the file now carries (replaced
#     content, `document_alias`) never uses its page and reaches only the Units the file covered
#     when it was written (`covered_units`); one with no such list reaches nothing;
#   * a `device` position never says `read` by itself (a look at the last page is a position, not
#     a reading): at most `in_progress`. A Unit is read by a `server` position (unless the member
#     undid it: `undone_units` is a local overlay that wins), or by a live entry of `read_units`,
#     the journal of a plausible pass over it (ADR-0053, speed rule on `server_ts`);
#   * per reader and Unit, the furthest wins: state, then exact before approximate, then the
#     fraction of the UNIT reached (never the percent of two different files), then the bounded
#     `client_ts`, then the id. A Unit that was read never goes back to unread, unless the member
#     undoes the mark (`undone_units`).
READING_PROGRESS_SELECT = """
SELECT user_id, unit_id,
  CASE WHEN srank = 2 THEN 'read' WHEN srank = 1 THEN 'in_progress' ELSE 'unread' END AS state,
  position_id AS last_position_id,
  latest_position_id,
  approximate
FROM (
  SELECT user_id, unit_id, position_id, srank, approximate,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, unit_id
      ORDER BY srank DESC, approximate ASC, coalesce(within, -1) DESC, ts DESC, position_id DESC
    ) AS rn,
    FIRST_VALUE(position_id) OVER (
      PARTITION BY user_id, unit_id ORDER BY ts DESC, position_id DESC
    ) AS latest_position_id
  FROM (
    SELECT o.user_id AS user_id, o.unit_id AS unit_id, o.position_id AS position_id,
      min(o.client_ts, o.server_ts) AS ts,
      CASE
        WHEN o.exact THEN CASE WHEN o.end_page > o.start_page
          THEN min(1.0, max(0.0, (o.page - o.start_page) * 1.0 / (o.end_page - o.start_page)))
          WHEN o.page >= o.end_page THEN 1.0 ELSE 0.0 END
        WHEN o.percent IS NULL THEN NULL
        ELSE min(1.0, max(0.0, o.percent * o.n - o.rank0)) END AS within,
      CASE WHEN o.done AND o.source = 'server'
          AND json_type(o.undone_units, '$."' || o.unit_id || '"') IS NULL THEN 2
        WHEN o.done OR o.started THEN 1
        ELSE 0 END AS srank,
      CASE WHEN o.replaced THEN 1 WHEN o.exact THEN 0 ELSE o.n > 1 OR o.percent IS NULL END
        AS approximate
    FROM (
      SELECT x.*,
        CASE WHEN NOT x.replaced AND x.page IS NOT NULL AND x.start_page IS NOT NULL
          AND x.end_page IS NOT NULL THEN 1 ELSE 0 END AS exact,
        CASE WHEN NOT x.replaced AND x.page IS NOT NULL AND x.start_page IS NOT NULL
          AND x.end_page IS NOT NULL THEN x.page >= x.end_page
          ELSE coalesce(x.percent, 0) >= (x.rank0 + 1) * 1.0 / x.n END AS done,
        CASE WHEN NOT x.replaced AND x.page IS NOT NULL AND x.start_page IS NOT NULL
          AND x.end_page IS NOT NULL THEN x.page >= x.start_page
          ELSE coalesce(x.percent, 0) > x.rank0 * 1.0 / x.n END AS started
      FROM (
        SELECT q.id AS position_id, q.user_id AS user_id, c.unit_id AS unit_id,
          q.percent AS percent, q.client_ts AS client_ts, q.server_ts AS server_ts,
          q.source AS source, q.page AS page, q.replaced AS replaced,
          c.rank0 AS rank0, c.n AS n, c.start_page AS start_page, c.end_page AS end_page,
          q.undone_units AS undone_units
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units,
            CASE WHEN json_type(locator, '$.page') = 'integer'
              THEN json_extract(locator, '$.page') END AS page,
            coalesce(content_hash <> CASE carrier_type
              WHEN 'export_item' THEN coalesce(
                (SELECT ct.content_hash FROM export_item AS e
                   JOIN content AS ct ON ct.id = e.content_id WHERE e.id = carrier_id),
                (SELECT a.content_hash FROM document_alias AS a
                   WHERE a.export_item_id = carrier_id
                   ORDER BY a.created_at DESC, a.file_hash DESC LIMIT 1))
              ELSE (SELECT ct.content_hash FROM content AS ct WHERE ct.id = carrier_id)
            END, 0) AS replaced
          FROM reading_position
        ) AS q
        JOIN (
          SELECT carrier_type, carrier_id, unit_id,
            json_extract(start_locator, '$.page') AS start_page,
            json_extract(end_locator, '$.page') AS end_page,
            ROW_NUMBER() OVER (PARTITION BY carrier_type, carrier_id ORDER BY position) - 1
              AS rank0,
            COUNT(*) OVER (PARTITION BY carrier_type, carrier_id) AS n
          FROM unit_coverage
        ) AS c ON c.carrier_type = q.carrier_type AND c.carrier_id = q.carrier_id
        WHERE NOT q.replaced
        UNION ALL
        SELECT q.id, q.user_id, u.id, q.percent, q.client_ts, q.server_ts, q.source, q.page,
          1, CAST(j.key AS INTEGER), json_array_length(q.covered_units), NULL, NULL, q.undone_units
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units, NULL AS page,
            coalesce(content_hash <> CASE carrier_type
              WHEN 'export_item' THEN coalesce(
                (SELECT ct.content_hash FROM export_item AS e
                   JOIN content AS ct ON ct.id = e.content_id WHERE e.id = carrier_id),
                (SELECT a.content_hash FROM document_alias AS a
                   WHERE a.export_item_id = carrier_id
                   ORDER BY a.created_at DESC, a.file_hash DESC LIMIT 1))
              ELSE (SELECT ct.content_hash FROM content AS ct WHERE ct.id = carrier_id)
            END, 0) AS replaced
          FROM reading_position WHERE covered_units IS NOT NULL
        ) AS q, json_each(q.covered_units) AS j
        JOIN unit AS u ON u.id = j.value
        WHERE q.replaced
      ) AS x
    ) AS o
    UNION ALL
    SELECT p.user_id, j.value, p.id, min(p.client_ts, p.server_ts), 1.0, 2, 0
    FROM reading_position AS p, json_each(p.read_units) AS j
    JOIN unit AS u ON u.id = j.value
    WHERE json_type(p.undone_units, '$."' || j.value || '"') IS NULL
  )
) WHERE rn = 1
"""

add_view(reading_position, "reading_progress", READING_PROGRESS_SELECT)

# Where each synchronisation of a member's progress with a destination stopped (EF-USR-012, M4):
# one cursor per account, so one server being down never moves another's. NULL = never run.
progress_sync_cursor = sa.Table(
    "progress_sync_cursor",
    metadata,
    sa.Column("shelf_destination_id", sa.String(26), nullable=False),
    sa.Column("user_id", sa.String(26), nullable=False),
    sa.Column("last_pull_server_ts", UtcDateTime(), nullable=True),
    sa.Column("last_push_at", UtcDateTime(), nullable=True),
    sa.ForeignKeyConstraint(
        ["user_id", "shelf_destination_id"],
        ["destination_account.user_id", "destination_account.shelf_destination_id"],
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint("shelf_destination_id", "user_id"),
)

# What the synchronisation noted of each Kavita chapter of an account (M4 L14b, 0078): the page
# Shelfhold last PUSHED (so that the echo of its own push is never read back as news), the state it
# last SAW (Kavita's page and its own stamp, only compared for equality, never with Shelfhold's
# clock), and when the chapter was last synchronised (Shelfhold's clock). `account_ref` says which
# Kavita account and which key the note was made for (the row of an account survives a re-link, the
# note does not); `series_stamp` is what the series list said of the series at the last pass.
progress_sync_note = sa.Table(
    "progress_sync_note",
    metadata,
    sa.Column("shelf_destination_id", sa.String(26), nullable=False),
    sa.Column("user_id", sa.String(26), nullable=False),
    sa.Column("kavita_chapter_id", sa.Integer(), nullable=False),
    sa.Column("account_id", sa.String(26), nullable=False),
    sa.Column("account_ref", sa.String(), nullable=False),
    sa.Column("carrier_id", sa.String(26), nullable=False),
    sa.Column("kavita_series_id", sa.Integer(), nullable=True),
    sa.Column("series_stamp", sa.String(), nullable=True),
    sa.Column("pushed_page", sa.Integer(), nullable=True),
    sa.Column("pushed_at", UtcDateTime(), nullable=True),
    sa.Column("seen_page", sa.Integer(), nullable=True),
    sa.Column("seen_modified", sa.String(), nullable=True),
    sa.Column("synced_at", UtcDateTime(), nullable=False),
    sa.ForeignKeyConstraint(
        ["user_id", "shelf_destination_id"],
        ["destination_account.user_id", "destination_account.shelf_destination_id"],
        ondelete="CASCADE",
    ),
    sa.PrimaryKeyConstraint("shelf_destination_id", "user_id", "kavita_chapter_id"),
    ulid_check("account_id"),
    ulid_check("carrier_id"),
    sa.CheckConstraint('"pushed_page" IS NULL OR "pushed_page" >= 0', name="pushed_page_min"),
    sa.CheckConstraint('"seen_page" IS NULL OR "seen_page" >= 0', name="seen_page_min"),
)
