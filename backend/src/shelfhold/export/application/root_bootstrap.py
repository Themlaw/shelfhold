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

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.domain.path_map import PathMode, clean_absolute, within
from shelfhold.export.domain.physical_roots import SentinelDocument
from shelfhold.infrastructure.db import read_transaction, write_transaction
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import create_sentinel
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.infrastructure.fsops.sentinel import read_sentinel
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform

__all__ = [
    "FALLBACK_JURISDICTION",
    "RootPolicy",
    "RootRefusedError",
    "default_jurisdiction",
    "ensure_instance",
    "register_roots",
    "target_roots",
]

FALLBACK_JURISDICTION = "FR"  # when the locale names no country (the project's default language)


def default_jurisdiction(env: Mapping[str, str] | None = None) -> str:
    """The country of the instance, deduced from the locale (EF-SRC-024: `instance.jurisdiction`,
    default deduced from the locale, confirmed by `shelfhold config set instance.jurisdiction`)."""
    env = os.environ if env is None else env
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        match = re.match(r"^[a-z]{2,3}_([A-Z]{2})\b", env.get(name, ""))
        if match:
            return match.group(1)
    return FALLBACK_JURISDICTION


log = structlog.get_logger(__name__)


def ensure_instance(engine: Engine, *, jurisdiction: str | None = None) -> str:
    with read_transaction(engine) as connection:  # the usual start: the instance exists
        existing = connection.execute(
            sa.select(platform.instance.c.instance_id)
        ).scalar_one_or_none()
    if existing is not None:
        return str(existing)
    with write_transaction(engine) as connection:
        found = connection.execute(sa.select(platform.instance.c.instance_id)).scalar_one_or_none()
        if found is not None:  # made by another process since the look above
            return str(found)
        instance_id = new_ulid()
        chosen, confirmed = _chosen(connection)
        connection.execute(
            sa.insert(platform.instance).values(
                instance_id=instance_id,
                jurisdiction=jurisdiction or chosen,
                # D4: confirmed only when the admin set it (`config set`), never by the locale
                jurisdiction_confirmed_at=datetime.now(UTC) if confirmed else None,
            )
        )
        return instance_id


def _chosen(connection: sa.Connection) -> tuple[str, bool]:
    """The country the admin set (confirmed), else the one the locale suggests (not)."""
    stored = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == "instance.jurisdiction"
        )
    ).scalar_one_or_none()
    return (stored, True) if stored else (default_jurisdiction(), False)


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


class RootRefusedError(ValueError):
    """A folder that must not become a physical root (EF-EXP-059); nothing was stamped."""


@dataclass(frozen=True)
class RootPolicy:
    """Where a writable root may be: under the library, never on or around `/data` or `/store`."""

    library_dir: Path
    data_dir: Path
    store_dir: Path

    def check(self, path: str, mode: PathMode, known: Iterable[str] = ()) -> None:
        if path == clean_absolute(os.path.abspath(self.store_dir)):
            return  # the store is registered by the instance itself, outside the library
        for name, reserved in (("data", self.data_dir), ("store", self.store_dir)):
            fixed = clean_absolute(os.path.abspath(reserved))
            if within(path, fixed) is not None or within(fixed, path) is not None:
                raise RootRefusedError(f"{path} is, contains or lies in the {name} folder")
        library = clean_absolute(os.path.abspath(self.library_dir))
        if mode is PathMode.RW and within(path, library) is None:
            raise RootRefusedError(f"{path} is not under the library folder {library}")
        for other in known:
            if other != path and (
                within(path, other) is not None or within(other, path) is not None
            ):
                raise RootRefusedError(f"{path} overlaps the root {other}")

    def check_all(self, wanted: Iterable[tuple[str, PathMode]]) -> None:
        paths = [(clean_absolute(os.path.abspath(raw)), mode) for raw, mode in wanted]
        for index, (path, mode) in enumerate(paths):
            others = [p for j, (p, _m) in enumerate(paths) if j != index]
            self.check(path, mode, others)


def register_roots(
    engine: Engine,
    wanted: Iterable[tuple[str, PathMode]],
    *,
    policy: RootPolicy | None = None,
    jurisdiction: str | None = None,
) -> list[str]:
    """Register the roots not known yet; returns the new `root_id`s.

    Refused before anything is stamped: a folder outside the library, on or around `/data` or
    `/store`, or overlapping a registered root (`RootRefusedError`). Nothing is stamped while the
    instance is frozen (`FreezeState`). A folder that cannot be written is skipped with
    `root_bootstrap.unwritable`: the other roots, `/store` included, go on."""
    instance_id = ensure_instance(engine, jurisdiction=jurisdiction)
    wanted = [(clean_absolute(os.path.abspath(raw)), mode) for raw, mode in wanted]
    if policy is not None:
        policy.check_all(wanted)
    with engine.connect() as connection:
        frozen = FreezeState.read(connection).frozen
        known = [
            clean_absolute(p)
            for p in connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars()
        ]
    created: list[str] = []
    for path, mode in wanted:
        if not Path(path).is_dir():
            log.info("root_bootstrap.folder_missing", path=path)
            continue
        if any(within(path, root) is not None for root in known):
            continue  # inside a registered root
        if policy is not None:
            policy.check(path, mode, known)  # a parent over a registered child is refused too
        if frozen:
            log.warning("root_bootstrap.frozen", path=path)  # restored_behind: nothing is stamped
            continue
        try:
            root_id = _stamp(path, mode, instance_id)
            if root_id is None:
                continue
            with write_transaction(engine) as connection:
                connection.execute(
                    sa.insert(platform.physical_root).values(
                        root_id=root_id, path_entry=path, mode=mode.value, generation=1
                    )
                )
        except OSError as error:
            log.warning("root_bootstrap.unwritable", path=path, error=str(error))
            continue
        known.append(path)
        created.append(root_id)
        log.info("root_bootstrap.registered", path=path, mode=mode.value)
    return created


def _stamp(path: str, mode: PathMode, instance_id: str) -> str | None:
    root_id = new_ulid()
    if mode is not PathMode.RW:
        return root_id
    with ConfinedRoot.open(path) as root:
        stamped = read_sentinel(root)
    if stamped is not None and stamped.instance_id == instance_id:
        return stamped.root_id  # a crash left it stamped: adopt it as it is
    if stamped is None:
        create_sentinel(
            path, SentinelDocument(root_id=root_id, instance_id=instance_id, generation=1)
        )
        return root_id
    log.warning("root_bootstrap.foreign_sentinel", path=path)  # `root takeover` (M1b) is the exit
    return None
