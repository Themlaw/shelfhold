"""ENF-SEC-013: the Kavita admin key is sealed at rest and read only by the core."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.export.targets.kavita import AdminKey, load_admin_key, store_admin_key
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import metadata
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.vault import SecretVault

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
export_target = metadata.tables["export_target"]
KEY = "synthetic-admin-key-0002"


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


@pytest.mark.req("ENF-SEC-013", "ENF-SEC-005")
def test_admin_key_is_sealed_in_the_target_row_and_opened_by_the_vault(engine: Engine) -> None:
    vault = SecretVault(Keyring.generate(NOW))
    target_id = new_ulid()
    with engine.begin() as connection:
        registry.sync_registry(connection, vault.keyring)
        connection.execute(sa.insert(export_target).values(id=target_id, kind="kavita", name="k"))
        store_admin_key(vault, connection, target_id, AdminKey(KEY))
    with engine.connect() as connection:
        row = connection.execute(sa.select(export_target)).mappings().one()
        assert KEY.encode() not in bytes(row["secret_ct"])  # sealed
        assert KEY not in str(dict(row["config"]))  # and not in the clear config either
        opened = load_admin_key(vault, connection, target_id)
    assert opened.reveal() == KEY
    assert KEY not in repr(opened) and KEY not in str(opened)
