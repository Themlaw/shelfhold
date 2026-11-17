"""Migration 0088 (M4 L21a, EF-BIB-013, ENF-SEC-005): `provider_secret`, the sealed keys of the
metadata providers (Hardcover, Google Books).

One table, one row per provider, the key in the four sealed columns of the secret vault; nothing
else is touched; reversible. The head of the migrations is pinned by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from shelfhold.infrastructure.secrets import registry
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "ENF-SEC-005", "EF-BIB-013")


def test_the_revision_is_0088_on_0087() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0088").down_revision == "0087"


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0087")
    assert "provider_secret" not in tables(database)
    assert "0088" in migrate(settings).applied
    assert "provider_secret" in tables(database)
    command.downgrade(alembic_config(database), "0087")
    assert "provider_secret" not in tables(database)


def test_the_table_is_a_sealed_table_the_rotation_finds_alone() -> None:
    sealed = registry.sealed_tables()["provider_secret"]
    assert sealed.pk.name == "provider"


def test_a_row_is_one_provider_with_a_whole_envelope_or_none(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        for bad in ("", "x" * 100):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO provider_secret (provider) VALUES (?)", (bad,))
        conn.execute("INSERT INTO provider_secret (provider) VALUES ('hardcover')")
        with pytest.raises(sqlite3.IntegrityError):  # one row per provider
            conn.execute("INSERT INTO provider_secret (provider) VALUES ('hardcover')")
        columns = {r[1] for r in conn.execute("PRAGMA table_info(provider_secret)")}
        assert {"secret_kid", "secret_v", "secret_nonce", "secret_ct"} <= columns
        assert not {c for c in columns if "key" in c and c != "secret_kid"}  # nothing in the clear
