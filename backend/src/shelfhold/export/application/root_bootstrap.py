"""First registration of the instance and of its physical roots (EF-EXP-059, EF-EXP-060).

Nothing else writes the `instance` row or the `physical_root` rows: a headless install (`target
add`, `config set path_map.*`, `shelf create`) and the server's start call this, and it is
idempotent. A writable root is stamped with its sentinel (`.shelfhold-mount-id`) *before* its row
is committed, so a crash between the two leaves a stamped folder that the next call adopts again
(same instance) instead of a row that points to an unstamped one. A `adopt_ro` root is a row
only: it is never written. A folder that does not exist yet is left for the next call; a root
already inside a registered one is not registered twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.domain.path_map import PathMode, clean_absolute, within
from shelfhold.export.domain.physical_roots import SentinelDocument
from shelfhold.infrastructure.fsops import create_sentinel
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.fsops.sentinel import read_sentinel
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform

__all__ = ["DEFAULT_JURISDICTION", "ensure_instance", "register_roots", "target_roots"]

DEFAULT_JURISDICTION = "FR"
log = structlog.get_logger(__name__)


def ensure_instance(engine: Engine, *, jurisdiction: str = DEFAULT_JURISDICTION) -> str:
    with engine.begin() as connection:
        found = connection.execute(sa.select(platform.instance.c.instance_id)).scalar_one_or_none()
        if found is not None:
            return str(found)
        instance_id = new_ulid()
        connection.execute(
            sa.insert(platform.instance).values(instance_id=instance_id, jurisdiction=jurisdiction)
        )
        return instance_id


def target_roots(engine: Engine) -> list[tuple[str, PathMode]]:
    """Every folder the path maps of the targets name, with its mode, in a stable order."""
    seen: dict[str, PathMode] = {}
    with engine.connect() as connection:
        for path_map in connection.execute(sa.select(export.export_target.c.path_map)).scalars():
            for entry in path_map or []:
                local = clean_absolute(entry["local"])
                mode = PathMode(entry.get("mode", "rw"))
                if seen.get(local) is not PathMode.RW:  # a writable declaration wins
                    seen[local] = mode
    return sorted(seen.items())


def register_roots(
    engine: Engine,
    wanted: Iterable[tuple[str, PathMode]],
    *,
    jurisdiction: str = DEFAULT_JURISDICTION,
) -> list[str]:
    """Register the roots not known yet; returns the new `root_id`s."""
    instance_id = ensure_instance(engine, jurisdiction=jurisdiction)
    created: list[str] = []
    for path, mode in wanted:
        if not Path(path).is_dir():
            log.info("root_bootstrap.folder_missing", path=path)
            continue
        with engine.connect() as connection:
            known = [
                clean_absolute(p)
                for p in connection.execute(
                    sa.select(platform.physical_root.c.path_entry)
                ).scalars()
            ]
        if any(within(path, root) is not None for root in known):
            continue  # inside a registered root
        root_id = new_ulid()
        if mode is PathMode.RW:
            with ConfinedRoot.open(path) as root:
                stamped = read_sentinel(root)
            if stamped is not None and stamped.instance_id == instance_id:
                root_id = stamped.root_id  # a crash left it stamped: adopt it as it is
            elif stamped is None:
                create_sentinel(
                    path, SentinelDocument(root_id=root_id, instance_id=instance_id, generation=1)
                )
            else:  # another instance's sentinel: `root takeover` is the exit (M1b), never here
                log.warning("root_bootstrap.foreign_sentinel", path=path)
                continue
        with engine.begin() as connection:
            connection.execute(
                sa.insert(platform.physical_root).values(
                    root_id=root_id, path_entry=path, mode=mode.value, generation=1
                )
            )
        created.append(root_id)
        log.info("root_bootstrap.registered", path=path, mode=mode.value)
    return created
