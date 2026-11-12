"""Export the followed series as OPML or CSV (EF-INT-027 (3)).

Both formats carry the same four fields: title, source, series URL and last number read. Neither
is a contract: the file is meant to be re-imported by hand elsewhere, so it stays plain text.
The URL is the one the source gave for the series, stripped of anything that could authenticate
(user info, fragment, and query parameters that look like a token, key or session).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import sqlalchemy as sa

from shelfhold.infrastructure.schema import acquisition, catalog

FORMATS = ("opml", "csv")
CSV_HEADER = ("title", "source", "url", "last_number")

_SENSITIVE_PARAM = re.compile(
    r"token|key|secret|sig|auth|pass|pwd|session|cookie|jwt|bearer|credential|code", re.IGNORECASE
)
# Characters XML 1.0 cannot carry at all, not even escaped.
_XML_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_XML_ESCAPES = {
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
    "\n": "&#10;", "\r": "&#13;", "\t": "&#9;",
}  # fmt: skip
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class FollowRow:
    title: str
    source: str
    url: str
    last_number: str  # empty when nothing was read yet


def public_url(raw: str | None) -> str:
    """The series URL without user info, fragment or credential-looking query parameters.

    Anything that is not an http(s) URL is dropped: the export never carries a bare identifier
    that a source might use as a capability.
    """
    if not raw:
        return ""
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{port}" if port else host
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not _SENSITIVE_PARAM.search(k)]
    )  # fmt: skip
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def collect_follows(engine: sa.Engine) -> list[FollowRow]:
    """One row per followed Edition, by title: its best active binding, its last read Unit."""
    sf, sb, si = acquisition.series_follow, acquisition.source_binding, acquisition.source_item
    work, unit = catalog.work, catalog.unit
    with engine.connect() as connection:
        follows = connection.execute(
            sa.select(sf.c.edition_id, work.c.title).join(work, work.c.id == sf.c.work_id)
        ).all()
        bindings: dict[str, tuple[str, str | None]] = {}
        for edition_id, source_key, url in connection.execute(
            sa.select(sb.c.edition_id, si.c.source_key, si.c.url)
            .join(si, si.c.id == sb.c.source_item_id)
            .where(sb.c.active.is_(True))
            .order_by(sb.c.priority.desc())  # the lowest priority number wins: written last
        ):
            bindings[edition_id] = (source_key, url)
        last_read: dict[str, str] = {}
        for edition_id, number in connection.execute(
            sa.select(unit.c.edition_id, unit.c.number_norm)
            .where(
                unit.c.track == "main",
                unit.c.id.in_(sa.text("SELECT unit_id FROM reading_progress WHERE state = 'read'")),
            )
            .order_by(unit.c.number_sort.asc(), unit.c.number_segment_ordinal.asc())
        ):
            last_read[edition_id] = number  # the highest number is written last
    rows = []
    for edition_id, title in follows:
        source_key, url = bindings.get(edition_id, ("", None))
        rows.append(FollowRow(title, source_key, public_url(url), last_read.get(edition_id, "")))
    return sorted(rows, key=lambda r: (r.title.casefold(), r.source, r.url))


def render_opml(rows: Iterable[FollowRow]) -> str:
    """OPML 2.0, one `outline` per series. Written by hand: the only XML entry point of the
    project is the parser (ENF-SEC-014), and every value is escaped as an attribute."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head>",
        "    <title>Shelfhold follows</title>",
        "  </head>",
        "  <body>",
    ]
    for row in rows:
        attributes = (
            ("text", row.title),
            ("type", "link"),
            ("url", row.url),
            ("source", row.source),
            ("lastNumber", row.last_number),
        )
        text = " ".join(f'{name}="{_attribute(value)}"' for name, value in attributes)
        lines.append(f"    <outline {text} />")
    lines += ["  </body>", "</opml>", ""]
    return "\n".join(lines)


def render_csv(rows: Iterable[FollowRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            [_cell(row.title), _cell(row.source), _cell(row.url), _cell(row.last_number)]
        )
    return buffer.getvalue()


def export_follows(engine: sa.Engine, fmt: str) -> str:
    """The followed series as `opml` or `csv` text; any other format is a `ValueError`."""
    if fmt not in FORMATS:
        raise ValueError(fmt)
    rows = collect_follows(engine)
    return render_opml(rows) if fmt == "opml" else render_csv(rows)


def _attribute(value: str) -> str:
    """Escaped for a double-quoted attribute; line breaks and tabs are kept as references."""
    escaped = _XML_FORBIDDEN.sub("", value)
    for raw, reference in _XML_ESCAPES.items():
        escaped = escaped.replace(raw, reference)
    return escaped


def _cell(value: str) -> str:
    """A title that a spreadsheet would run as a formula is kept as text (leading quote)."""
    return "'" + value if value.startswith(_FORMULA_START) else value
