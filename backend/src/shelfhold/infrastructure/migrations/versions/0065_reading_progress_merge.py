"""The view `reading_progress` becomes a merge of the positions of a reader (M4 L14a).

Until now the view took the latest position by `server_ts` whatever its progress, so a book
reopened at the start (or an old backup, or a reader that sent late what it had read before) put a
read Unit back to unread (M1a, decision 10). The view now keeps, per reader and Unit, the furthest
position (`reading.domain.merge`); a position on a replaced content is `approximate` and reaches
only the Units its file covered when it was written; a reader's own report never marks a Unit read
by itself, a page-by-page traversal does (ADR-0053). EF-LEC-014, EF-BIB-026.

`reading_position` gets four columns (`source`, `covered_units`, `read_units`, `undone_units`) and
a check: `page`, when there is one, is an integer (a text sorts above every number in SQLite). A
page already written as something else is removed from its locator first: the position stays, its
page is ignored. The view holds no data; the downgrade restores the view of the 0004 and drops the
columns.
"""

# ruff: noqa: E501  (frozen, generated constraint expressions)

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0071"
branch_labels = None
depends_on = None

MERGED = """
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
      CASE WHEN o.done AND o.source = 'server' THEN 2 WHEN o.done OR o.started THEN 1
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
          c.rank0 AS rank0, c.n AS n, c.start_page AS start_page, c.end_page AS end_page
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units,
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
          1, CAST(j.key AS INTEGER), json_array_length(q.covered_units), NULL, NULL
        FROM (
          SELECT id, user_id, carrier_type, carrier_id, percent, client_ts, server_ts, source,
            covered_units, NULL AS page,
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

_CHECKS = {
    "ck_reading_position_source_values": "\"source\" IN ('device', 'server')",
    "ck_reading_position_covered_units_list": '"covered_units" IS NULL OR (json_valid("covered_units") AND json_type("covered_units") = \'array\')',
    "ck_reading_position_read_units_list": 'json_valid("read_units") AND json_type("read_units") = \'array\'',
    "ck_reading_position_undone_units_object": 'json_valid("undone_units") AND json_type("undone_units") = \'object\'',
    "ck_reading_position_page_integer": "json_type(\"locator\", '$.page') IS NULL OR json_type(\"locator\", '$.page') = 'integer'",
}


def _restated() -> tuple[sa.Column[Any], ...]:
    """What reflection would render differently: the clock default."""
    return (
        sa.Column(
            "server_ts", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress")
    op.execute(
        "UPDATE reading_position SET locator = json_remove(locator, '$.page') "
        "WHERE json_type(locator, '$.page') IS NOT NULL AND json_type(locator, '$.page') <> 'integer'"
    )
    with op.batch_alter_table(
        "reading_position", recreate="always", reflect_args=_restated()
    ) as batch:
        batch.add_column(sa.Column("source", sa.String(), server_default="device", nullable=False))
        batch.add_column(sa.Column("covered_units", sa.JSON(none_as_null=True), nullable=True))
        batch.add_column(sa.Column("read_units", sa.JSON(), server_default="[]", nullable=False))
        batch.add_column(sa.Column("undone_units", sa.JSON(), server_default="{}", nullable=False))
        for name, expression in _CHECKS.items():
            batch.create_check_constraint(op.f(name), expression)
    op.execute(f"CREATE VIEW reading_progress AS {MERGED}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress")
    with op.batch_alter_table(
        "reading_position", recreate="always", reflect_args=_restated()
    ) as batch:
        for name in _CHECKS:
            batch.drop_constraint(op.f(name), type_="check")
        for column in ("undone_units", "read_units", "covered_units", "source"):
            batch.drop_column(column)
    op.execute(f"CREATE VIEW reading_progress AS {LATEST}")
