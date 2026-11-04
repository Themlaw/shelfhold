"""ENF-SEC-005: registry of keys, resumable rotation, restored pre-rotation copy, cited kids."""

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import metadata
from shelfhold.infrastructure.secrets import registry
from shelfhold.infrastructure.secrets.errors import (
    DecryptionError,
    KeyRetirementRefusedError,
    RotationInProgressError,
)
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore
from shelfhold.infrastructure.secrets.rotation import retire_key, rotate
from shelfhold.infrastructure.secrets.vault import SecretVault

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
source_config = metadata.tables["source_config"]
secret_key = metadata.tables["secret_key"]
tombstone_kid = metadata.tables["tombstone_kid"]


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    yield engine
    engine.dispose()


@pytest.fixture
def store(settings: Settings) -> KeyringStore:
    store = KeyringStore(
        settings.data_dir / "keyring", lock_path=settings.data_dir / "keyring.lock"
    )
    store.create(Keyring.generate(NOW))
    return store


def seed(engine: Engine, store: KeyringStore, count: int) -> list[str]:
    vault = SecretVault(store.load())
    ids = [f"plugin-{index:02d}" for index in range(count)]
    with engine.begin() as connection:
        registry.sync_registry(connection, vault.keyring)
        for plugin_id in ids:
            connection.execute(sa.insert(source_config).values(plugin_id=plugin_id))
            vault.seal_row(connection, "source_config", plugin_id, f"secret-{plugin_id}".encode())
    return ids


def kids_in_use(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(sa.select(source_config.c.secret_kid)).scalars())


@pytest.mark.req("ENF-SEC-005")
def test_sealed_tables_are_discovered_from_the_schema() -> None:
    assert {"source_config", "source_account"} <= set(registry.sealed_tables())


@pytest.mark.req("ENF-SEC-005")
def test_registry_mirrors_the_keyring_states(engine: Engine, store: KeyringStore) -> None:
    keyring = store.load().rotated(LATER)
    with engine.begin() as connection:
        registry.sync_registry(connection, keyring)
        registry.sync_registry(connection, keyring)  # idempotent
        rows = {
            row.kid: (row.state, row.retired_at, row.alg)
            for row in connection.execute(sa.select(secret_key))
        }
    assert rows[keyring.active.kid][0] == "active"
    retired = next(k for k in keyring.keys if k.state == "retired")
    assert rows[retired.kid] == ("retired", LATER, "xchacha20poly1305")


@pytest.mark.req("ENF-SEC-005")
def test_row_seal_and_open_round_trip_and_bind_to_the_row(
    engine: Engine, store: KeyringStore
) -> None:
    seed(engine, store, 2)
    vault = SecretVault(store.load())
    with engine.begin() as connection:
        assert vault.open_row(connection, "source_config", "plugin-00") == b"secret-plugin-00"
        # copy the sealed columns of one row onto the other: it no longer opens
        first = connection.execute(
            sa.select(source_config).where(source_config.c.plugin_id == "plugin-00")
        ).one()
        connection.execute(
            sa.update(source_config)
            .where(source_config.c.plugin_id == "plugin-01")
            .values(
                secret_kid=first.secret_kid,
                secret_v=first.secret_v,
                secret_nonce=first.secret_nonce,
                secret_ct=first.secret_ct,
            )
        )
        with pytest.raises(DecryptionError):
            vault.open_row(connection, "source_config", "plugin-01")


@pytest.mark.req("ENF-SEC-005")
def test_secret_rotation_reencrypts_every_row_under_the_new_key(
    engine: Engine, store: KeyringStore
) -> None:
    ids = seed(engine, store, 5)
    old_kid = store.load().active.kid
    report = rotate(store, engine, now=LATER)
    keyring = store.load()
    assert report.new_kid == keyring.active.kid != old_kid
    assert report.reencrypted == 5
    assert report.resumed is False
    assert keyring.rotation is None
    assert keyring.get(old_kid).state == "retired"
    assert kids_in_use(engine) == {keyring.active.kid}
    vault = SecretVault(keyring)
    with engine.connect() as connection:
        for plugin_id in ids:
            assert (
                vault.open_row(connection, "source_config", plugin_id)
                == f"secret-{plugin_id}".encode()
            )


@pytest.mark.req("ENF-SEC-005")
def test_secret_rotation_interrupted_is_resumable(engine: Engine, store: KeyringStore) -> None:
    ids = seed(engine, store, 6)
    old_kid = store.load().active.kid
    done: list[str] = []

    def crash_after_third(table: str, row_id: str) -> None:
        done.append(row_id)
        if len(done) == 3:
            raise KeyboardInterrupt("simulated kill")

    with pytest.raises(KeyboardInterrupt):
        rotate(store, engine, now=LATER, on_row=crash_after_third)

    # halfway: the intention is durable, both keys are kept, every row is readable
    midway = store.load()
    assert midway.rotation is not None
    assert midway.get(old_kid).state == "retired"
    assert len(kids_in_use(engine)) == 2
    vault = SecretVault(midway)
    with engine.connect() as connection:
        for plugin_id in ids:
            assert vault.open_row(connection, "source_config", plugin_id)

    report = rotate(store, engine, now=LATER)  # same command, resumed
    final = store.load()
    assert report.resumed is True
    assert report.new_kid == midway.rotation.to_kid == final.active.kid
    assert report.reencrypted == 3
    assert len(final.keys) == 2  # no third key was created by the resume
    assert final.rotation is None
    assert kids_in_use(engine) == {final.active.kid}


@pytest.mark.req("ENF-SEC-005")
def test_resume_after_a_crash_before_any_row_was_touched(
    engine: Engine, store: KeyringStore
) -> None:
    seed(engine, store, 2)

    def crash(_table: str, _row_id: str) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        rotate(store, engine, now=LATER, on_row=crash)
    assert store.load().rotation is not None
    assert rotate(store, engine, now=LATER).resumed is True
    assert len(store.load().keys) == 2


@pytest.mark.req("ENF-SEC-005")
def test_rotation_with_nothing_to_reencrypt_still_rotates(
    engine: Engine, store: KeyringStore
) -> None:
    old_kid = store.load().active.kid
    report = rotate(store, engine, now=LATER)
    assert report.reencrypted == 0
    assert store.load().active.kid != old_kid


@pytest.mark.req("ENF-SEC-005")
def test_concurrent_rotations_are_serialised_by_the_lock(
    engine: Engine, store: KeyringStore
) -> None:
    seed(engine, store, 1)

    def nested(_table: str, _row_id: str) -> None:
        with pytest.raises(RotationInProgressError):
            rotate(store, engine, now=LATER)

    rotate(store, engine, now=LATER, on_row=nested)


@pytest.mark.req("ENF-SEC-005")
def test_restore_pre_rotation_copy_decrypts_with_retired_kid(
    engine: Engine, store: KeyringStore, settings: Settings, tmp_path: Path
) -> None:
    seed(engine, store, 3)
    with engine.connect() as connection:  # a consistent copy of /data before the rotation
        connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    snapshot = tmp_path / "pre-rotation.db"
    shutil.copy(settings.database_path, snapshot)
    old_kid = store.load().active.kid

    rotate(store, engine, now=LATER)

    restored = create_db_engine(snapshot)
    try:
        vault = SecretVault(store.load())  # the keyring written AFTER the rotation
        with restored.connect() as connection:
            row = connection.execute(sa.select(source_config.c.secret_kid)).first()
            assert row is not None
            assert row.secret_kid == old_kid != vault.keyring.active.kid
            assert vault.open_row(connection, "source_config", "plugin-00") == b"secret-plugin-00"
    finally:
        restored.dispose()


@pytest.mark.req("ENF-SEC-005")
def test_cited_tombstone_kid_never_retired(engine: Engine, store: KeyringStore) -> None:
    cited = store.load().active.kid
    rotate(store, engine, now=LATER)
    with engine.begin() as connection:
        connection.execute(sa.insert(tombstone_kid).values(kid=cited, first_seq=1))
    with pytest.raises(KeyRetirementRefusedError) as info:
        retire_key(store, engine, cited)
    assert info.value.reason == "cited_by_tombstone"
    assert store.load().get(cited).material  # still there
    # and the registry itself refuses to lose the row (trigger of the schema)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(sa.delete(secret_key).where(secret_key.c.kid == cited))


@pytest.mark.req("ENF-SEC-005")
def test_active_or_in_use_key_is_never_retired(engine: Engine, store: KeyringStore) -> None:
    seed(engine, store, 2)
    active = store.load().active.kid
    with pytest.raises(KeyRetirementRefusedError) as info:
        retire_key(store, engine, active)
    assert info.value.reason == "active"

    # a half-finished rotation: the old key still seals a row
    def crash(_table: str, _row_id: str) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        rotate(store, engine, now=LATER, on_row=crash)
    with pytest.raises(KeyRetirementRefusedError) as info:
        retire_key(store, engine, active)
    assert info.value.reason == "in_use"


@pytest.mark.req("ENF-SEC-005")
def test_unused_uncited_retired_key_can_be_dropped(engine: Engine, store: KeyringStore) -> None:
    old = store.load().active.kid
    rotate(store, engine, now=LATER)
    retire_key(store, engine, old)
    assert old not in {k.kid for k in store.load().keys}
    with pytest.raises(KeyRetirementRefusedError) as info:
        retire_key(store, engine, "k-unknown")
    assert info.value.reason == "unknown"


@pytest.mark.req("ENF-SEC-005")
def test_rotation_never_touches_the_tombstone_table(engine: Engine, store: KeyringStore) -> None:
    sql = "SELECT count(*) FROM content_tombstone"
    with engine.connect() as connection:
        before = connection.exec_driver_sql(sql).scalar_one()
    rotate(store, engine, now=LATER)
    with engine.connect() as connection:
        assert connection.exec_driver_sql(sql).scalar_one() == before


@pytest.mark.req("ENF-SEC-005")
def test_review_rows_left_under_a_retired_key_are_reported(
    engine: Engine, store: KeyringStore, settings: Settings
) -> None:
    from dataclasses import replace

    from shelfhold.infrastructure.secrets import diagnostics

    seed(engine, store, 2)
    first = store.load()
    rotate(store, engine, now=LATER)
    assert "rows_on_retired_key" not in {w.code for w in diagnostics.collect_warnings(settings)}
    # a process that kept the pre-rotation keyring in memory seals under the retired key
    stale = replace(
        first,
        keys=tuple(
            replace(k, state="active", retired_at=None)
            if k.kid == first.active.kid
            else replace(k, state="retired", retired_at=LATER)
            for k in store.load().keys
        ),
        rotation=None,
    )
    with engine.begin() as connection:
        SecretVault(stale).seal_row(connection, "source_config", "plugin-00", b"late")
    warnings = {w.code: w for w in diagnostics.collect_warnings(settings)}
    assert warnings["rows_on_retired_key"].detail == "1"
    # the retired key is therefore still in use and cannot be dropped
    with pytest.raises(KeyRetirementRefusedError) as info:
        retire_key(store, engine, first.active.kid)
    assert info.value.reason == "in_use"
    rotate(store, engine, now=LATER)  # a second run catches the straggler
    assert "rows_on_retired_key" not in {w.code for w in diagnostics.collect_warnings(settings)}
