"""M6 L09b, passe 11 (EF-BIB-054 (4), EF-SRC-030): a `replaced_by` migration consults the
tombstones.

The index may name a plugin's replacement (`replaced_by`); the replacement is never installed by
that word alone (another key is a new install, with its own checks), but the admin can migrate the
old plugin's sources to it. The migration is an identity migration: every living line is asked
about as a family of references — its new form under the replacing plugin and its old form (the
inverse alias) — and a line that would meet a tombstone, whichever form it got there by, is
blocked and signalled (`tombstone.migration_hit`); nothing is recreated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, Credential, Principal
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.interfaces.local_cli.ops import OPS_METHODS
from shelfhold.library.public import SeriesBindingRef
from shelfhold.ops.plugin_migration import PluginMigrationOps
from shelfhold.ops.source_migration import SourceMigrationOps
from tests.integration.test_l27_source_aliases import GONE, PLUGIN, SOURCE, alias, erase, live
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-BIB-054", "EF-SRC-030")

ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)
REPLACEMENT = "plugin-b"


def _items(env: object) -> list[tuple[str, str]]:
    with env.inst.engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                acquisition.source_item.c.plugin_id, acquisition.source_item.c.remote_id
            ).order_by(acquisition.source_item.c.remote_id)
        ).all()
    return sorted((row.plugin_id, row.remote_id) for row in rows)


def test_identity_migration_consults_tombstones(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        gone = live(env, GONE)  # the series that will be erased
        live(env, "keep-1")  # the series the migration may move
        live(env, "hit-1")  # its old form resolves, by an alias, to the erased reference
        alias(env, "hit-1", GONE)
        erase(env, monkeypatch, settings, gone)  # the purge takes the erased series' own row

        # the reserved action and its door: the host CLI through the operating channel
        assert "plugin.migrate_to" in RESERVED_ACTIONS
        assert "ops.plugin_migrate_to" in OPS_METHODS

        report = PluginMigrationOps(env.inst.comp).migrate_to(
            key=PLUGIN, replacement=REPLACEMENT, principal=ADMIN
        )
        assert report == {"moved": 1, "blocked": 1}

        # the blocked line stayed under the old plugin — nothing was recreated for the erased
        # series — and the clean one moved to the replacement
        assert _items(env) == [(PLUGIN, "hit-1"), (REPLACEMENT, "keep-1")]

        # the block came from the old form (the inverse alias), and the migration sealed the
        # new forms under the replacing key: the erased series, and the line the alias held
        # back, stay blocked under the replacement too — it acquires nothing the old plugin
        # had lost
        service = SourceMigrationOps(env.inst.comp)._service()
        assert service.check(SeriesBindingRef(REPLACEMENT, SOURCE, GONE), root_id=None).blocked
        assert service.check(SeriesBindingRef(REPLACEMENT, SOURCE, "hit-1"), root_id=None).blocked
        assert service.check(SeriesBindingRef(PLUGIN, SOURCE, "hit-1"), root_id=None).blocked

        # the hit was told, once, with the scope only
        hits = [e for e in env.rows(platform.event) if e.type == "tombstone.migration_hit"]
        assert hits and {json.dumps(e.payload) for e in hits} == {'{"scope": "series_binding"}'}


def test_replaced_by_migration_refuses_the_same_key_and_the_unknown(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        live(env, "keep-1")
        ops = PluginMigrationOps(env.inst.comp)
        with pytest.raises(Exception) as raised:
            ops.migrate_to(key=PLUGIN, replacement=PLUGIN, principal=ADMIN)
        assert "same_key" in str(raised.value)
        with pytest.raises(Exception) as raised:
            ops.migrate_to(key="nobody", replacement=REPLACEMENT, principal=ADMIN)
        assert "unknown" in str(raised.value)
        assert _items(env) == [(PLUGIN, "keep-1")]  # nothing moved
