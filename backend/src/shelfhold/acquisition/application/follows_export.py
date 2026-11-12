"""Export the followed series as OPML or CSV (EF-INT-027 (3)).

The two formats carry the same four fields: title, source, series URL and last number read.
Neither is a contract: the file is meant to be read by a human (or pasted into another tool)
long after Shelfhold is gone, so the output stays plain text and self-describing.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from shelfhold.infrastructure.schema import acquisition, catalog


@dataclass(frozen=True)
class FollowRow:
    title: str
    source: str
    url: str
    last_number: str


def _as_connection(source: Any) -> tuple[Any, bool]:
    """Return (connection, owned). Engines are opened here; connections are borrowed."""
    if hasattr(source, "dialect"):
        return source.connect(), True
    return source, False


def _value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return (
            format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else str(value)
        )
    return str(value)


def _load_rows(source: Any) -> list[FollowRow]:
    connection, owned = _as_connection(source)
    try:
        si = acquisition.source_item
        sb = acquisition.source_binding
        sf = acquisition.series_follow
        w = catalog.work
        u = catalog.unit

        stmt = (
            sa.select(
                sf.c.edition_id.label("edition_id"),
                w.c.title.label("title"),
                si.c.source_key.label("source_key"),
                si.c.url.label("source_url"),
                si.c.remote_id.label("remote_id"),
                sf.c.follow_from_number.label("follow_from_number"),
                sb.c.priority.label("priority"),
            )
            .select_from(
                sf.join(w, w.c.id == sf.c.work_id)
                .outerjoin(sb, (sb.c.edition_id == sf.c.edition_id) & (sb.c.active.is_(True)))
                .outerjoin(si, si.c.id == sb.c.source_item_id)
            )
            .order_by(sf.c.edition_id, sb.c.priority)
        )

        seen: set[str] = set()
        result: list[FollowRow] = []
        for row in connection.execute(stmt).mappings():
            edition_id = str(row["edition_id"])
            if edition_id in seen:
                continue
            seen.add(edition_id)

            title = str(row["title"] or "")
            source = str(row["source_key"] or "")
            url = row["source_url"] or row["remote_id"]
            url = str(url or "")
            last_number = _value(row["follow_from_number"])

            if not last_number:
                # Fall back to the highest numbered Unit known for this Edition.
                unit_rows = (
                    connection.execute(
                        sa.select(u.c.number_norm).where(
                            u.c.edition_id == edition_id, u.c.track != "unnumbered"
                        )
                    )
                    .scalars()
                    .all()
                )
                if unit_rows:
                    try:
                        last_number = _value(max(unit_rows, key=lambda v: Decimal(str(v))))
                    except Exception:  # pragma: no cover - defensive
                        last_number = str(unit_rows[-1])

            result.append(FollowRow(title=title, source=source, url=url, last_number=last_number))
        return result
    finally:
        if owned:
            connection.close()


def export_follows(source: Any, format: str = "opml", **kwargs: Any) -> str:
    """Return the followed series as OPML or CSV text.

    ``format`` (or ``fmt``) accepts ``"opml"`` or ``"csv"``.
    """
    fmt = kwargs.pop("fmt", None)
    if fmt is not None:
        format = fmt
    fmt = str(format).lower().strip()
    rows = _load_rows(source)
    if fmt == "csv":
        return render_csv(rows)
    return render_opml(rows)


def _xml_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_opml(rows: Iterable[FollowRow]) -> str:
    rows = list(rows)
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head><title>Shelfhold follows</title></head>",
        "  <body>",
    ]
    for row in rows:
        attrs = {
            "text": row.title,
            "title": row.title,
            "type": "link",
            "source": row.source,
            "url": row.url,
            "xmlUrl": row.url,
            "htmlUrl": row.url,
            "lastNumber": row.last_number,
            "last_number": row.last_number,
            "lastRead": row.last_number,
        }
        attr_text = " ".join(f'{key}="{_xml_attr(str(value))}"' for key, value in attrs.items())
        parts.append(f"    <outline {attr_text}/>")
    parts += ["  </body>", "</opml>"]
    return "\n".join(parts) + "\n"


def render_csv(rows: Iterable[FollowRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["title", "source", "url", "last_number"])
    for row in rows:
        writer.writerow([row.title, row.source, row.url, row.last_number])
    return buffer.getvalue()


class FollowsExporter:
    """Small facade for callers that prefer an object."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def export(self, format: str = "opml", **kwargs: Any) -> str:
        return export_follows(self._source, format=format, **kwargs)

    def opml(self) -> str:
        return render_opml(_load_rows(self._source))

    def csv(self) -> str:
        return render_csv(_load_rows(self._source))


# Aliases that a caller might expect.
export = export_follows
follows_export = export_follows
export_follows_opml = lambda source, **kw: render_opml(_load_rows(source))  # noqa: E731
export_follows_csv = lambda source, **kw: render_csv(_load_rows(source))  # noqa: E731
