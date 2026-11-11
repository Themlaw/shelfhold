"""Migration 0027 (M2 L07, EF-SRC-013, EF-EXP-065): `source_secret`, one sealed value per source
and field, and the plugin version an attestation was given for."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("EF-SRC-013", "EF-EXP-065", "ENF-OPS-002")


def previous_revision() -> str:
    """The head of main when 0027 lands: the test follows `down_revision`, not a number."""
    down = ScriptDirectory(str(default_script_location())).get_revision("0027").down_revision
    assert isinstance(down, str)
    return down


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def test_upgrade_adds_source_secret_and_attestation_version(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO plugin_attestation (plugin_key, field, value, attested_at) "
            "VALUES ('acme', 'content_trust', 'publisher', '2026-11-01 00:00:00')"
        )
        db.commit()

    result = migrate(settings)

    assert "0027" in result.applied
    assert "source_secret" in tables(database)
    with closing(sqlite3.connect(database)) as db:
        assert "plugin_version" in _columns(db, "plugin_attestation")
        # an attestation from before the version was kept is bound to no version: it lapses
        assert db.execute("SELECT plugin_version FROM plugin_attestation").fetchall() == [("",)]
        with pytest.raises(sqlite3.IntegrityError):  # the id is the associated data: no drift
            db.execute(
                "INSERT INTO source_secret (id, plugin_id, kind, field, fingerprint, "
                "secret_kid, secret_v, secret_nonce, secret_ct) "
                "VALUES ('bravo/secret/x', 'acme', 'secret', 'x', 'abcd0123', NULL, NULL, NULL, "
                "NULL)"
            )
        with pytest.raises(sqlite3.IntegrityError):  # a row with no sealed value is no secret
            db.execute(
                "INSERT INTO source_secret (id, plugin_id, kind, field, fingerprint) "
                "VALUES ('acme/secret/x', 'acme', 'secret', 'x', 'abcd0123')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO source_secret (id, plugin_id, kind, field, fingerprint, "
                "secret_kid, secret_v, secret_nonce, secret_ct) "
                "VALUES ('acme/other/x', 'acme', 'other', 'x', 'abcd0123', 'k', 1, x'00', "
                "zeroblob(16))"
            )

    command.downgrade(alembic_config(database), previous_revision())
    assert "source_secret" not in tables(database)
    with closing(sqlite3.connect(database)) as db:
        assert "plugin_version" not in _columns(db, "plugin_attestation")
    assert current_revision(database) == previous_revision()
