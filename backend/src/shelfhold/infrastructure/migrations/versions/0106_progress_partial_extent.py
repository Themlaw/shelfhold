"""`reading_progress` reads `unit_coverage.extent`: part of a Unit is at most in progress (M5 L22a).

A file that covers a Unit only in part (`extent <> 'full'`: the first half of a chapter at the end
of a volume) says nothing of the rest of it. The view gave `read` to such a Unit when a server said
the file read, and a plausible walk through its pages journaled the Unit read (`read_units`): both
are now `in_progress`, unless another file covers the Unit whole. Its fraction is the one of the
Unit (the pages the file holds over the span of a file that covers it whole), never of the covered
part alone. The mark a walk already journaled for a partial Unit (written by an earlier version)
is not read either; a carried-over mark (`transposed`) is not about a file and is kept.

The view holds no data and its columns do not change (frozen since 1.1); `reading_progress_proven`
(the one criterion of what leaves for Suwayomi and Kavita) is the same rule over the positions that
are not transposed. Reversible: the downgrade restores the two views of 0084.

Number: 0105 is L21's; chained on 0104, the head of main when this lot was merged.
"""

from alembic import op

revision = "0106"
down_revision = "0104"
branch_labels = None
depends_on = None

PARTIAL_EXTENT = """
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
        WHEN o.extent <> 'full' THEN CASE
          WHEN o.exact AND o.end_page > o.start_page AND o.unit_span > 0
          THEN min(1.0, max(0.0, (o.page - o.start_page) * 1.0 / o.unit_span)) END
        WHEN o.exact THEN CASE WHEN o.end_page > o.start_page
          THEN min(1.0, max(0.0, (o.page - o.start_page) * 1.0 / (o.end_page - o.start_page)))
          WHEN o.page >= o.end_page THEN 1.0 ELSE 0.0 END
        WHEN o.percent IS NULL THEN NULL
        ELSE min(1.0, max(0.0, o.percent * o.n - o.rank0)) END AS within,
      CASE WHEN o.done AND o.source = 'server' AND o.extent = 'full'
          AND json_type(o.undone_units, '$."' || o.unit_id || '"') IS NULL THEN 2
        WHEN o.done OR o.started THEN 1
        ELSE 0 END AS srank,
      CASE WHEN o.replaced OR o.transposed THEN 1 WHEN o.exact THEN 0
        ELSE o.n > 1 OR o.percent IS NULL END
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
          q.undone_units AS undone_units, q.transposed AS transposed, c.extent AS extent,
          c.unit_span AS unit_span
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
          SELECT carrier_type, carrier_id, unit_id, extent,
            (SELECT max(json_extract(u2.end_locator, '$.page')
                        - json_extract(u2.start_locator, '$.page'))
               FROM unit_coverage AS u2
              WHERE u2.unit_id = unit_coverage.unit_id AND u2.extent = 'full') AS unit_span,
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
          1, CAST(j.key AS INTEGER), json_array_length(q.covered_units), NULL, NULL,
          q.undone_units, q.transposed, 'full', NULL
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units, NULL AS page,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
    SELECT p.user_id, j.value, p.id, min(p.client_ts, p.server_ts), 1.0, 2,
      coalesce(json_extract(p.locator, '$.transposed'), 0)
    FROM reading_position AS p, json_each(p.read_units) AS j
    JOIN unit AS u ON u.id = j.value
    WHERE json_type(p.undone_units, '$."' || j.value || '"') IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM unit_coverage AS uc
        WHERE uc.carrier_type = p.carrier_type AND uc.carrier_id = p.carrier_id
          AND uc.unit_id = j.value AND uc.extent <> 'full'
          AND coalesce(json_extract(p.locator, '$.transposed'), 0) = 0)
  )
) WHERE rn = 1
"""

PARTIAL_EXTENT_PROVEN = """
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
        WHEN o.extent <> 'full' THEN CASE
          WHEN o.exact AND o.end_page > o.start_page AND o.unit_span > 0
          THEN min(1.0, max(0.0, (o.page - o.start_page) * 1.0 / o.unit_span)) END
        WHEN o.exact THEN CASE WHEN o.end_page > o.start_page
          THEN min(1.0, max(0.0, (o.page - o.start_page) * 1.0 / (o.end_page - o.start_page)))
          WHEN o.page >= o.end_page THEN 1.0 ELSE 0.0 END
        WHEN o.percent IS NULL THEN NULL
        ELSE min(1.0, max(0.0, o.percent * o.n - o.rank0)) END AS within,
      CASE WHEN o.done AND o.source = 'server' AND o.extent = 'full'
          AND json_type(o.undone_units, '$."' || o.unit_id || '"') IS NULL THEN 2
        WHEN o.done OR o.started THEN 1
        ELSE 0 END AS srank,
      CASE WHEN o.replaced OR o.transposed THEN 1 WHEN o.exact THEN 0
        ELSE o.n > 1 OR o.percent IS NULL END
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
          q.undone_units AS undone_units, q.transposed AS transposed, c.extent AS extent,
          c.unit_span AS unit_span
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
          WHERE coalesce(json_extract(locator, '$.transposed'), 0) = 0
        ) AS q
        JOIN (
          SELECT carrier_type, carrier_id, unit_id, extent,
            (SELECT max(json_extract(u2.end_locator, '$.page')
                        - json_extract(u2.start_locator, '$.page'))
               FROM unit_coverage AS u2
              WHERE u2.unit_id = unit_coverage.unit_id AND u2.extent = 'full') AS unit_span,
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
          1, CAST(j.key AS INTEGER), json_array_length(q.covered_units), NULL, NULL,
          q.undone_units, q.transposed, 'full', NULL
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units, NULL AS page,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
          AND coalesce(json_extract(locator, '$.transposed'), 0) = 0
        ) AS q, json_each(q.covered_units) AS j
        JOIN unit AS u ON u.id = j.value
        WHERE q.replaced
      ) AS x
    ) AS o
    UNION ALL
    SELECT p.user_id, j.value, p.id, min(p.client_ts, p.server_ts), 1.0, 2,
      coalesce(json_extract(p.locator, '$.transposed'), 0)
    FROM reading_position AS p, json_each(p.read_units) AS j
    JOIN unit AS u ON u.id = j.value
    WHERE coalesce(json_extract(p.locator, '$.transposed'), 0) = 0
      AND json_type(p.undone_units, '$."' || j.value || '"') IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM unit_coverage AS uc
        WHERE uc.carrier_type = p.carrier_type AND uc.carrier_id = p.carrier_id
          AND uc.unit_id = j.value AND uc.extent <> 'full'
          AND coalesce(json_extract(p.locator, '$.transposed'), 0) = 0)
  )
) WHERE rn = 1
"""

TRANSPOSED_APPROXIMATE = """
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
      CASE WHEN o.replaced OR o.transposed THEN 1 WHEN o.exact THEN 0
        ELSE o.n > 1 OR o.percent IS NULL END
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
          q.undone_units AS undone_units, q.transposed AS transposed
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
          1, CAST(j.key AS INTEGER), json_array_length(q.covered_units), NULL, NULL,
          q.undone_units, q.transposed
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units, NULL AS page,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
    SELECT p.user_id, j.value, p.id, min(p.client_ts, p.server_ts), 1.0, 2,
      coalesce(json_extract(p.locator, '$.transposed'), 0)
    FROM reading_position AS p, json_each(p.read_units) AS j
    JOIN unit AS u ON u.id = j.value
    WHERE json_type(p.undone_units, '$."' || j.value || '"') IS NULL
  )
) WHERE rn = 1
"""

PROVEN = """
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
      CASE WHEN o.replaced OR o.transposed THEN 1 WHEN o.exact THEN 0
        ELSE o.n > 1 OR o.percent IS NULL END
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
          q.undone_units AS undone_units, q.transposed AS transposed
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
          WHERE coalesce(json_extract(locator, '$.transposed'), 0) = 0
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
          1, CAST(j.key AS INTEGER), json_array_length(q.covered_units), NULL, NULL,
          q.undone_units, q.transposed
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, undone_units, NULL AS page,
            coalesce(json_extract(locator, '$.transposed'), 0) AS transposed,
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
          AND coalesce(json_extract(locator, '$.transposed'), 0) = 0
        ) AS q, json_each(q.covered_units) AS j
        JOIN unit AS u ON u.id = j.value
        WHERE q.replaced
      ) AS x
    ) AS o
    UNION ALL
    SELECT p.user_id, j.value, p.id, min(p.client_ts, p.server_ts), 1.0, 2,
      coalesce(json_extract(p.locator, '$.transposed'), 0)
    FROM reading_position AS p, json_each(p.read_units) AS j
    JOIN unit AS u ON u.id = j.value
    WHERE coalesce(json_extract(p.locator, '$.transposed'), 0) = 0
      AND json_type(p.undone_units, '$."' || j.value || '"') IS NULL
  )
) WHERE rn = 1
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress_proven")
    op.execute("DROP VIEW IF EXISTS reading_progress")
    op.execute(f"CREATE VIEW reading_progress AS {PARTIAL_EXTENT}")
    op.execute(f"CREATE VIEW reading_progress_proven AS {PARTIAL_EXTENT_PROVEN}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress_proven")
    op.execute("DROP VIEW IF EXISTS reading_progress")
    op.execute(f"CREATE VIEW reading_progress AS {TRANSPOSED_APPROXIMATE}")
    op.execute(f"CREATE VIEW reading_progress_proven AS {PROVEN}")
