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
    ulid_check("id"),
    ulid_check("carrier_id"),
    in_check("carrier_type", POSITION_CARRIERS),
    prefixed_hash_check("content_hash"),
    json_shape_check("locator", "object"),
    sa.CheckConstraint('"percent" IS NULL OR "percent" BETWEEN 0 AND 1', name="percent_range"),
    sa.Index("ix_reading_position_user", "user_id", "carrier_type", "carrier_id", "server_ts"),
    sa.Index("ix_reading_position_content_hash", "content_hash"),
)

# What a reader got to in a Unit, merged over every position that reaches it (EF-LEC-014,
# EF-LEC-007). `reading.domain` states the rule and `test_the_view_is_the_domain_rule` proves the
# view equal to it:
#   * per position and Unit: exact with known pages, else proportional to the Unit's rank among the
#     Units of the file and `approximate`, except for a file that holds one Unit; a position whose
#     `content_hash` is not the one the file now carries (replaced content, `document_alias`)
#     never uses its page and is always `approximate` (I30);
#   * per reader and Unit: the furthest wins, by state, then percent, then `client_ts` bounded by
#     `server_ts`, then id. A Unit that was read never goes back to unread.
# The Unit's state and `last_position_id` are those of the winning position. The relays of L14b,
# L47 and L50 read this view and write positions only; they never write the view.
READING_PROGRESS_SELECT = """
SELECT user_id, unit_id,
  CASE WHEN srank = 2 THEN 'read' WHEN srank = 1 THEN 'in_progress' ELSE 'unread' END AS state,
  position_id AS last_position_id,
  approximate
FROM (
  SELECT user_id, unit_id, position_id, srank, approximate,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, unit_id
      ORDER BY srank DESC, coalesce(percent, -1) DESC, min(client_ts, server_ts) DESC,
        position_id DESC
    ) AS rn
  FROM (
    SELECT p.user_id AS user_id, p.cover_unit AS unit_id, p.id AS position_id,
      p.percent AS percent, p.client_ts AS client_ts, p.server_ts AS server_ts,
      CASE WHEN p.done THEN 2 WHEN p.started THEN 1 ELSE 0 END AS srank,
      CASE WHEN p.replaced THEN 1 WHEN p.exact THEN 0 ELSE p.n > 1 OR p.percent IS NULL END
        AS approximate
    FROM (
      SELECT q.*,
        CASE WHEN NOT q.replaced AND q.page IS NOT NULL AND c.start_page IS NOT NULL
          AND c.end_page IS NOT NULL THEN 1 ELSE 0 END AS exact,
        CASE WHEN NOT q.replaced AND q.page IS NOT NULL AND c.start_page IS NOT NULL
          AND c.end_page IS NOT NULL THEN q.page >= c.end_page
          ELSE coalesce(q.percent, 0) >= (c.rank0 + 1) * 1.0 / c.n END AS done,
        CASE WHEN NOT q.replaced AND q.page IS NOT NULL AND c.start_page IS NOT NULL
          AND c.end_page IS NOT NULL THEN q.page >= c.start_page
          ELSE coalesce(q.percent, 0) > c.rank0 * 1.0 / c.n END AS started,
        c.unit_id AS cover_unit, c.n AS n
      FROM (
        SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts,
          json_extract(locator, '$.page') AS page,
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
    ) AS p
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
