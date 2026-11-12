"""Seeds and helpers of the Health page tests (M3 L15): rows written the way the services leave
them, no disk, no network."""

import argparse
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, storage
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.interfaces.cli.registry import build_parser
from tests.auth_env import AuthEnv, db_rows
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)

HEALTH_PATH = "/api/v1/health/page"
SHA = "sha256:" + "ab" * 32
OTHER_SHA = "sha256:" + "cd" * 32
HOST_CANARY = "canary-host.invalid"  # a service URL that only an administrator may read
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def add_profile(api: AuthEnv, username: str, role: str, ceiling: int | None = None) -> str:
    """A member or a child, with the administrator's password hash."""
    (admin_hash,) = db_rows(api.settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
    profile_id = new_ulid()
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate, max_age_rating) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (profile_id, username, admin_hash, role, ceiling),
        )
        connection.commit()
    finally:
        connection.close()
    return profile_id


def engine_of(settings: Settings) -> sa.Engine:
    return create_db_engine(settings.database_path)


def seed_sources(settings: Settings) -> None:
    """Three installed sources: one healthy, one out of reach for three cycles (degraded), one
    whose session expired (paused). The settings of one hold a service URL."""
    engine = engine_of(settings)
    try:
        with engine.begin() as connection:
            for plugin, health, extra in (
                ("good-source", {"outage": 1}, {}),
                ("down-source", {"outage": 3, "unavailable": True}, {}),
                (
                    "expired-source",
                    {
                        "pause": {
                            "failure_class": "auth",
                            "event_type": "source.auth_expired",
                            "waiting_state": "auth_expired",
                            "since": NOW.isoformat(),
                        }
                    },
                    {"settings": {"base_url": f"https://{HOST_CANARY}/api"}},
                ),
            ):
                connection.execute(
                    sa.insert(acquisition.source_config).values(
                        plugin_id=plugin, health=health, **extra
                    )
                )
    finally:
        engine.dispose()


def _content(
    connection: sa.Connection, unit_id: str, *, provenance: str = "received", **extra: Any
) -> str:
    content_id = new_ulid()
    connection.execute(
        sa.insert(storage.content).values(
            id=content_id, unit_id=unit_id, kind="images", media_type="application/epub+zip",
            format="epub", retention="adopted_ref", original_sha256=SHA, content_hash=SHA,
            store_provenance=provenance, size=10, **extra,
        )
    )  # fmt: skip
    return content_id


def _kept_copy(connection: sa.Connection, unit_id: str, rel_path: str, ino: int) -> None:
    connection.execute(
        sa.insert(replica_schema.replica).values(
            id=new_ulid(), unit_id=unit_id, location="store", rel_path=rel_path, dev=1, ino=ino,
            content_hash=SHA, independent=True, fidelity="original",
        )
    )  # fmt: skip


def seed_units(settings: Settings) -> dict[str, str]:
    """Four Units: `replaceable` (an active source lists it), `bare` (irreplaceable, no copy),
    `single` (irreplaceable, one independent copy in /store), `kept` (irreplaceable, two)."""
    engine = engine_of(settings)
    ids: dict[str, str] = {}
    try:
        with engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            binding = add_binding(connection, work, edition, add_source_item(connection))
            ids["replaceable"] = add_unit(connection, edition, "1")
            add_variant(connection, ids["replaceable"], binding, "ch-1")
            for number, name in (("2", "bare"), ("3", "single"), ("4", "kept")):
                ids[name] = add_unit(connection, edition, number)
            _content(connection, ids["single"])
            _kept_copy(connection, ids["single"], "single/one.epub", 11)
            _content(connection, ids["kept"])
            _kept_copy(connection, ids["kept"], "kept/one.epub", 21)
            _kept_copy(connection, ids["kept"], "kept/two.epub", 22)
    finally:
        engine.dispose()
    return ids


def seed_lost_original(settings: Settings) -> tuple[str, str]:
    """An `untrusted` EPUB whose original is gone: only the neutralised derivative is held."""
    engine = engine_of(settings)
    try:
        with engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            unit = add_unit(connection, edition, "9")
            content = _content(
                connection, unit, provenance="extracted_derived", derived_hash=OTHER_SHA,
                derived_by="xhtml-neutralizer@v1",
            )  # fmt: skip
            keep = add_unit(connection, edition, "10")
            _content(connection, keep)  # a received original: nothing lost
    finally:
        engine.dispose()
    return unit, content


def seed_requests(settings: Settings, now: datetime, ids: dict[str, str]) -> None:
    """`ana`: 3 requests, 2 delivered; `leo`: 1 cancelled; an operation (no profile) and a
    request older than the window, which count for nobody."""
    engine = engine_of(settings)
    try:
        with engine.begin() as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            binding = add_binding(connection, work, edition, add_source_item(connection, "r"))
            for index, (who, state, age) in enumerate(
                (
                    (ids["ana"], "fetched", 2),
                    (ids["ana"], "fetched", 3),
                    (ids["ana"], "requested", 1),
                    (ids["leo"], "cancelled", 4),
                    (None, "fetched", 2),
                    (ids["ana"], "fetched", 40),
                )
            ):
                unit = add_unit(connection, edition, str(100 + index))
                variant = add_variant(connection, unit, binding, f"r-{index}")
                connection.execute(
                    sa.insert(acquisition.download_request).values(
                        id=new_ulid(), target_kind="unit", variant_id=variant, unit_id=unit,
                        source_binding_id=binding, state=state,
                        requested_at=now - timedelta(days=age), requester_user_id=who,
                    )
                )  # fmt: skip
    finally:
        engine.dispose()


def cli_exists(command: str) -> bool:
    """Is `shelfhold <group> <action> ...` a command of the parser? The words after the leaf
    (identifiers, `--flags`, `<placeholders>`) are its arguments and are not looked at."""
    words = command.split()
    assert words[0] == "shelfhold", command
    parser = build_parser()
    for index, word in enumerate(words[1:]):
        sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if sub is None:
            return index > 0  # a leaf: what follows is an argument
        if word not in sub.choices:
            return False
        parser = sub.choices[word]
    return len(words) > 1
