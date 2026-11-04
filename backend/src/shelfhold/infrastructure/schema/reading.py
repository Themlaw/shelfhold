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

# A Unit's state comes from the latest position on any carrier that covers it. With known bounds
# (`page` in the locator and in the coverage) it is exact; otherwise it is proportional to the
# Unit's rank among the carrier's Units and flagged `approximate`, except for a carrier that
# holds one Unit.
READING_PROGRESS_SELECT = """
SELECT user_id, unit_id,
  CASE WHEN done THEN 'read' WHEN started THEN 'in_progress' ELSE 'unread' END AS state,
  position_id AS last_position_id,
  approximate
FROM (
  SELECT p.user_id AS user_id, c.unit_id AS unit_id, p.id AS position_id,
    ROW_NUMBER() OVER (
      PARTITION BY p.user_id, c.unit_id ORDER BY p.server_ts DESC, p.id DESC
    ) AS rn,
    CASE WHEN c.start_page IS NOT NULL AND c.end_page IS NOT NULL AND p.page IS NOT NULL
      THEN p.page >= c.end_page
      ELSE coalesce(p.percent, 0) >= (c.rank0 + 1) * 1.0 / c.n END AS done,
    CASE WHEN c.start_page IS NOT NULL AND c.end_page IS NOT NULL AND p.page IS NOT NULL
      THEN p.page >= c.start_page
      ELSE coalesce(p.percent, 0) > c.rank0 * 1.0 / c.n END AS started,
    CASE WHEN c.start_page IS NOT NULL AND c.end_page IS NOT NULL AND p.page IS NOT NULL
      THEN 0 ELSE c.n > 1 OR p.percent IS NULL END AS approximate
  FROM (
    SELECT id, user_id, carrier_type, carrier_id, percent, server_ts,
      json_extract(locator, '$.page') AS page
    FROM reading_position
  ) AS p
  JOIN (
    SELECT carrier_type, carrier_id, unit_id,
      json_extract(start_locator, '$.page') AS start_page,
      json_extract(end_locator, '$.page') AS end_page,
      ROW_NUMBER() OVER (PARTITION BY carrier_type, carrier_id ORDER BY position) - 1 AS rank0,
      COUNT(*) OVER (PARTITION BY carrier_type, carrier_id) AS n
    FROM unit_coverage
  ) AS c ON c.carrier_type = p.carrier_type AND c.carrier_id = p.carrier_id
) WHERE rn = 1
"""

add_view(reading_position, "reading_progress", READING_PROGRESS_SELECT)
