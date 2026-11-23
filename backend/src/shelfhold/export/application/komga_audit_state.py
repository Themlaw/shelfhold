"""What the audit of a Komga proved, kept between two calls (EF-EXP-047, L08b).

One `app_setting` row per target. The state names the **folders** that were proved (with the
lowest age they were proved for) and, when nothing is proved, the closed code of the reason. No
account name, no key. The gate reads it at each binding and at each planning: a proof that is not
there, that is for another folder or for a lower ceiling, or that cannot be read, is no proof.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.domain.bindings import effective_partitioning
from shelfhold.export.domain.komga_access import (
    AUDIT_STALE,
    FOLDER_NOT_AUDITED,
    LIBRARY_ABOVE_RATING,
    NEVER_AUDITED,
    NO_CEILING,
)
from shelfhold.export.domain.path_map import PathMapError, clean_absolute
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.schema import platform

__all__ = [
    "MAX_AGE",
    "audit_reason",
    "exposure_for",
    "load",
    "partitioning_of",
    "proved_for",
    "remote_root_of",
    "save",
    "use_clock",
]

MAX_AGE: Final = timedelta(hours=36)  # a day and a margin: an older proof is no proof (#320 M2)
_clock: Clock = SystemClock()


def use_clock(clock: Clock | None) -> None:
    """The clock the gate reads freshness on. The gate is called from places that hold no clock
    (every binding, every planning), so the composition root poses it once."""
    global _clock
    _clock = clock or SystemClock()


def _fresh(state: dict[str, Any]) -> bool:
    try:
        at = datetime.fromisoformat(str(state.get("audited_at")))
        return at.tzinfo is not None and _clock.now() - at <= MAX_AGE
    except ValueError:
        return False


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
    if not isinstance(proved, list) or not remote_root or not _fresh(state):
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


def exposure_for(
    connection: Connection, target_id: str, remote_root: str, ceiling: int | None
) -> str | None:
    """A fresh audit saw a child's account reach a library over this folder at an age below the
    ceiling (below any age for a shelf of 18+): the closed code, else None. This is looked at for
    **every** shelf bound to a Komga, not only those below 18+ (#320 M6)."""
    state = load(connection, target_id)
    if not remote_root or not _fresh(state):
        return None
    reach, libraries = state.get("child_libraries"), state.get("libraries")
    if not isinstance(reach, dict) or not isinstance(libraries, list):
        return None
    wanted = NO_CEILING if ceiling is None else ceiling
    for lib in libraries:
        if not isinstance(lib, dict) or not isinstance(lib.get("root"), str):
            continue
        base = [p for p in lib["root"].split("/") if p]
        parts = [p for p in remote_root.split("/") if p]
        age = reach.get(lib.get("ref"))
        if parts[: len(base)] == base and isinstance(age, int) and age < wanted:
            return LIBRARY_ABOVE_RATING
    return None


def audit_reason(connection: Connection, target_id: str, remote_root: str | None = None) -> str:
    """Why a folder of this target is not proved: a closed code, for the refusal and for the
    admin. With `remote_root`, the reason of **that** folder — its own finding when the audit
    kept one (a complete audit keeps a finding per folder), never the gap of another folder;
    a folder no finding names was simply not audited (`folder_not_audited`). Without it, the
    reason of the target: a failure or a refusal of the reading, the first gap otherwise."""
    state = load(connection, target_id)
    if not state:
        return NEVER_AUDITED
    if not _fresh(state):
        return AUDIT_STALE
    findings = state.get("findings")
    if remote_root and isinstance(findings, list) and findings:
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            code = finding.get("code")
            if finding.get("folder") == remote_root and isinstance(code, str):
                return code
        return FOLDER_NOT_AUDITED  # the gaps kept are other folders': this one was not audited
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
