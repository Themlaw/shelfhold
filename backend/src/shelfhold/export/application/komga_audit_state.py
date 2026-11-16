"""What the audit of a Komga proved, kept between two calls (EF-EXP-047, L08b).

One `app_setting` row per target. The state names the **folders** that were proved (with the
lowest age they were proved for) and, when nothing is proved, the closed code of the reason. No
account name, no key. The gate reads it at each binding and at each planning: a proof that is not
there, that is for another folder or for a lower ceiling, or that cannot be read, is no proof.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.domain.bindings import effective_partitioning
from shelfhold.export.domain.komga_access import (
    FOLDER_NOT_AUDITED,
    NEVER_AUDITED,
    NO_CEILING,
)
from shelfhold.export.domain.path_map import PathMapError, clean_absolute
from shelfhold.infrastructure.schema import platform

__all__ = [
    "audit_reason",
    "load",
    "partitioning_of",
    "proved_for",
    "remote_root_of",
    "save",
]

_KEY = "komga.access_audit."


def load(connection: Connection, target_id: str) -> dict[str, Any]:
    table = platform.app_setting
    raw = connection.execute(
        sa.select(table.c.value).where(table.c.key == _KEY + target_id)
    ).scalar()
    try:
        state = json.loads(raw) if raw is not None else None
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def save(connection: Connection, target_id: str, state: dict[str, Any]) -> None:
    table = platform.app_setting
    value = json.dumps(state, sort_keys=True)
    done = connection.execute(
        sa.update(table).where(table.c.key == _KEY + target_id).values(value=value)
    )
    if done.rowcount == 0:
        connection.execute(sa.insert(table).values(key=_KEY + target_id, value=value))


def remote_root_of(path_map: Sequence[dict[str, Any]], root_subpath: str) -> str:
    """The folder Komga sees for a binding: the first writable entry plus the sub-folder. An empty
    string when there is none or the sub-folder climbs: that is no folder anyone proved."""
    for entry in path_map:
        if entry.get("mode", "rw") == "rw":
            try:
                return clean_absolute(f"{entry['remote']}/{root_subpath}" if root_subpath
                                      else str(entry["remote"]))  # fmt: skip
            except (PathMapError, KeyError, TypeError):
                return ""
    return ""


def proved_for(
    connection: Connection, target_id: str, remote_root: str, ceiling: int | None
) -> bool:
    """True only when the audit proved this exact folder for this ceiling or a higher one."""
    state = load(connection, target_id)
    proved = state.get("proved")
    if not isinstance(proved, list) or not remote_root:
        return False
    wanted = NO_CEILING if ceiling is None else ceiling
    for entry in proved:
        if not isinstance(entry, dict) or entry.get("root") != remote_root:
            continue
        have = entry.get("ceiling")
        have = NO_CEILING if have is None else have
        if isinstance(have, int) and not isinstance(have, bool) and wanted <= have:
            return True
    return False


def audit_reason(connection: Connection, target_id: str) -> str:
    """Why this target is not proved: a closed code, for the refusal and for the admin."""
    state = load(connection, target_id)
    if not state:
        return NEVER_AUDITED
    reason = state.get("reason")
    return reason if isinstance(reason, str) and reason else FOLDER_NOT_AUDITED


def partitioning_of(
    connection: Connection, *, target_id: str, kind: str, stored: str, attested: bool,
    path_map: Sequence[dict[str, Any]], root_subpath: str, ceiling: int | None,
) -> str:  # fmt: skip
    """`effective_partitioning` of one binding: a Komga counts only for a folder it proved."""
    proved = kind == "komga" and proved_for(
        connection, target_id, remote_root_of(path_map, root_subpath), ceiling
    )
    return effective_partitioning(kind, stored, attested=attested, audit_proved=proved)
