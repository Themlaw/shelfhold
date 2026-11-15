"""The view `reading_progress` becomes a merge of the positions of a reader (M4 L14a).

Until now the view took the latest position by `server_ts` whatever its progress, so a book
reopened at the start (or an old backup, or a reader that sent late what it had read before) put a
read Unit back to unread (M1a, decision 10). The view now keeps, per reader and Unit, the furthest
position: the highest state, then the highest percent, then the latest `client_ts` bounded by
`server_ts`. A position taken on a content that was replaced (the hash the file now carries
differs, `document_alias`) is `approximate` and never uses its page (EF-LEC-014, EF-BIB-026).

A view holds no data: no row moves, in either direction. The statements are frozen copies (a
migration never imports the schema it is about to be compared with).
"""

from alembic import op

revision = "0065"
down_revision = "0071"
branch_labels = None
depends_on = None

MERGED = """
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

LATEST = """
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


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress")
    op.execute(f"CREATE VIEW reading_progress AS {MERGED}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress")
    op.execute(f"CREATE VIEW reading_progress AS {LATEST}")
