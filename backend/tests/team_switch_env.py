"""A chapter served from one team, and the same chapter fetched from the preferred one (M4 L35).

Built on `export_env` and `upgrade_env`: real files, a real migrated database, the real pipeline and
the real same-path upgrade. The "upgrader" is the one of the follow cycle (`SamePathUpgrade.
upgrade_unit`), asked on the test's own context, as `runtime_jobs._LazyUpgrader` does on the
write context of a cycle.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from shelfhold.catalog.application.dto import BindingRef
from shelfhold.catalog.public import Catalog
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.library.public import PassthroughStore
from tests.export_env import ExportEnv
from tests.library_env import cbz_bytes
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED
from tests.upgrade_env import Exported, export_one, service

NOW = datetime(2026, 11, 17, 12, 0)


class RealUpgrader:
    """`_LazyUpgrader.upgrade_unit` without the lazy lease: every served file of the Unit goes
    through `SamePathUpgrade.upgrade`, and the outcomes are the words of the report."""

    def __init__(self, env: ExportEnv) -> None:
        self.env = env
        self.calls: list[tuple[str, str]] = []

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]:
        self.calls.append((unit_id, content_id))
        return [r.outcome.value for r in service(self.env).upgrade_unit(unit_id, content_id)]


@dataclass
class Switch:
    exported: Exported
    unit_id: str
    edition_id: str
    team_b: str
    variant_b: str
    content_b: str

    @property
    def target(self) -> Path:
        return self.exported.target


def add_team_chapter(
    env: ExportEnv, edition_id: str, binding_id: str, unit_id: str, number: str, team_id: str,
    *, fetched: bool = True, tag: bytes = b"team-b",
) -> tuple[str, str]:  # fmt: skip
    """A Variant of the Unit released by the team, received: `(variant id, content id)`."""
    variant = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(catalog.unit_variant).values(
                id=variant, unit_id=unit_id, source_binding_id=binding_id,
                remote_unit_id=f"r-{new_ulid()}", lang="fr", team_id=team_id,
            )
        )  # fmt: skip
        if fetched:
            connection.execute(
                sa.insert(acquisition.download_request).values(
                    id=new_ulid(), target_kind="unit", variant_id=variant,
                    source_binding_id=binding_id, unit_id=unit_id, state="fetched",
                    updated_at=NOW,
                )
            )  # fmt: skip
    pages = ((f"{number}-001.png", b"\x89PNG\r\n\x1a\n" + tag + b"\x00" * 32),)
    PassthroughStore(
        env.engine, env.lib.fsops, env.lib.locate, env.lib.data,
        tombstones=NONE_PURGED, hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=unit_id, variant_id=variant, staged=env.lib.stage(cbz_bytes(pages)),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{unit_id}-{new_ulid()}.cbz"),
    )  # fmt: skip
    with env.engine.connect() as connection:
        content = connection.execute(
            sa.text("SELECT id FROM content WHERE variant_id = :v"), {"v": variant}
        ).scalar_one()
    return variant, str(content)


def team_switch(env: ExportEnv, *, prefer: bool = True) -> Switch:
    """One chapter exported from a Variant with no team, and a Team B Variant of it, fetched; the
    Edition prefers Team B (`prefer=False` leaves the priority list alone)."""
    exported = export_one(env)
    edition_id = exported.series.edition_id
    with write_transaction(env.engine) as connection:
        served = connection.execute(  # the Variant the file was exported from is the preferred one
            sa.select(catalog.unit_variant.c.id).where(
                catalog.unit_variant.c.unit_id == exported.unit_id
            )
        ).scalar_one()
        Catalog(connection).set_preferred(exported.unit_id, str(served))
        team_b = Catalog(connection).resolve_team(
            BindingRef(exported.series.binding_id, edition_id), "Team B", NOW
        ).id
    variant_b, content_b = add_team_chapter(
        env, edition_id, exported.series.binding_id, exported.unit_id, "1", team_b
    )
    if prefer:
        with write_transaction(env.engine) as connection:
            Catalog(connection).prefer_team(edition_id, team_b)
    return Switch(exported, exported.unit_id, edition_id, team_b, variant_b, content_b)


def preferred_variant(env: ExportEnv, unit_id: str) -> str:
    with env.engine.connect() as connection:
        return str(
            connection.execute(
                sa.text("SELECT id FROM unit_variant WHERE unit_id = :u AND preferred"),
                {"u": unit_id},
            ).scalar_one()
        )


def count(env: ExportEnv, table: str) -> int:
    with env.engine.connect() as connection:
        return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
