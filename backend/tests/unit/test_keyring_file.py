"""ENF-SEC-005: the keyring file (0600, atomic, retired keys kept) and the boot checks."""

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shelfhold.config import Settings, load_settings
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import (
    KeyringCorruptError,
    KeyringMissingError,
    KeyringPermissionsError,
    KeyringUnreadableError,
)
from shelfhold.infrastructure.secrets.keyring import Keyring, KeyringStore

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def store_in(directory: Path) -> KeyringStore:
    directory.mkdir(parents=True, exist_ok=True)
    return KeyringStore(directory / "keyring", lock_path=directory / "keyring.lock")


@pytest.mark.req("ENF-SEC-005")
def test_keyring_is_created_0600_whatever_the_umask(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    previous = os.umask(0)
    try:
        store.create(Keyring.generate(NOW))
    finally:
        os.umask(previous)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name != "keyring.lock"] == ["keyring"]


@pytest.mark.req("ENF-SEC-005")
def test_keyring_creation_never_overwrites_an_existing_keyring(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    first = Keyring.generate(NOW)
    store.create(first)
    with pytest.raises(FileExistsError):
        store.create(Keyring.generate(NOW))
    assert store.load().active.kid == first.active.kid


@pytest.mark.req("ENF-SEC-005")
def test_keyring_creation_is_atomic_a_failed_write_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_in(tmp_path)

    def broken_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        store.create(Keyring.generate(NOW))
    assert not store.path.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name != "keyring.lock"] == []


@pytest.mark.req("ENF-SEC-005")
def test_keyring_rewrite_is_atomic_and_keeps_mode(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    keyring = Keyring.generate(NOW)
    store.create(keyring)
    store.write(keyring.rotated(LATER))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert len(store.load().keys) == 2


@pytest.mark.req("ENF-SEC-005")
def test_rotation_keeps_retired_keys_and_one_active(tmp_path: Path) -> None:
    first = Keyring.generate(NOW)
    second = first.rotated(LATER)
    assert second.active.kid != first.active.kid
    retired = second.get(first.active.kid)
    assert retired.state == "retired"
    assert retired.retired_at == LATER
    assert retired.material == first.active.material
    assert [k.state for k in second.keys].count("active") == 1
    store = store_in(tmp_path)
    store.create(second)
    assert store.load() == second


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b'{"format": 2, "keys": []}', b'{"format": 1, "keys": []}'],
)
def test_corrupt_or_unsupported_keyring_is_refused(tmp_path: Path, content: bytes) -> None:
    store = store_in(tmp_path)
    store.path.write_bytes(content)
    store.path.chmod(0o600)
    with pytest.raises(KeyringCorruptError):
        store.load()


def data_settings(tmp_path: Path, **extra: str) -> Settings:
    env = {
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        **extra,
    }
    return load_settings(env).values


@pytest.mark.req("ENF-SEC-005")
def test_first_boot_generates_the_keyring_in_data(tmp_path: Path) -> None:
    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    keyring = diagnostics.boot_check(settings)
    path = settings.data_dir / "keyring"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keyring.active.state == "active"
    assert diagnostics.boot_check(settings).active.kid == keyring.active.kid  # stable


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_secret_file_unreadable_by_uid_fails_boot(tmp_path: Path) -> None:
    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    diagnostics.boot_check(settings)
    path = settings.data_dir / "keyring"
    path.chmod(0o000)
    try:
        with pytest.raises(KeyringUnreadableError):
            diagnostics.boot_check(settings)
    finally:
        path.chmod(0o600)


@pytest.mark.req("ENF-SEC-005")
def test_secret_file_unreadable_is_refused_even_for_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    diagnostics.boot_check(settings)
    real_open = os.open

    def deny(path: str | os.PathLike[str], *args: object, **kwargs: object) -> int:
        if Path(path).name == "keyring":
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(KeyringUnreadableError):
        diagnostics.boot_check(settings)


@pytest.mark.req("ENF-SEC-005")
def test_secret_file_unreadable_fails_application_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from shelfhold.main import create_app

    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    diagnostics.boot_check(settings)
    real_open = os.open

    def deny(path: str | os.PathLike[str], *args: object, **kwargs: object) -> int:
        if Path(path).name == "keyring":
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(KeyringUnreadableError), TestClient(create_app(settings)):
        pass


@pytest.mark.req("ENF-SEC-005")
def test_configured_keyring_that_is_missing_fails_boot_instead_of_generating(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "run" / "keyring"
    settings = data_settings(tmp_path, SHELFHOLD_SECRET_KEY_FILE=str(outside))
    settings.data_dir.mkdir()
    with pytest.raises(KeyringMissingError):
        diagnostics.boot_check(settings)
    assert not outside.exists()


@pytest.mark.req("ENF-SEC-005")
def test_status_warns_master_key_inside_data(tmp_path: Path) -> None:
    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    diagnostics.boot_check(settings)
    codes = {w.code for w in diagnostics.collect_warnings(settings)}
    assert "key_in_data" in codes


@pytest.mark.req("ENF-SEC-005")
def test_status_does_not_warn_when_the_key_lives_outside_data(tmp_path: Path) -> None:
    outside = tmp_path / "run" / "keyring"
    outside.parent.mkdir()
    store = KeyringStore(outside, lock_path=tmp_path / "lock")
    store.create(Keyring.generate(NOW))
    settings = data_settings(tmp_path, SHELFHOLD_SECRET_KEY_FILE=str(outside))
    settings.data_dir.mkdir()
    codes = {w.code for w in diagnostics.collect_warnings(settings)}
    assert "key_in_data" not in codes


@pytest.mark.req("ENF-SEC-005")
def test_status_warns_after_rotation_until_a_new_export(tmp_path: Path) -> None:
    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    keyring = diagnostics.boot_check(settings)
    store = diagnostics.store_for(settings)
    assert "never_exported" in {w.code for w in diagnostics.collect_warnings(settings)}
    store.write(keyring.exported(NOW))
    assert "never_exported" not in {w.code for w in diagnostics.collect_warnings(settings)}
    store.write(store.load().rotated(LATER).rotation_completed(LATER))
    assert "export_older_than_rotation" in {w.code for w in diagnostics.collect_warnings(settings)}
    store.write(store.load().exported(LATER))
    assert "export_older_than_rotation" not in {
        w.code for w in diagnostics.collect_warnings(settings)
    }


# --- review of PR #34 ----------------------------------------------------------------------------


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o602, 0o666])
def test_review_keyring_with_loose_permissions_is_refused_with_the_fix(
    tmp_path: Path, mode: int
) -> None:
    store = store_in(tmp_path)
    store.create(Keyring.generate(NOW))
    store.path.chmod(mode)
    with pytest.raises(KeyringPermissionsError) as info:
        store.load()
    assert f"chmod 600 {store.path}" in str(info.value)


@pytest.mark.req("ENF-SEC-005")
def test_review_keyring_symlink_is_not_followed(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(Keyring.generate(NOW).to_json())
    elsewhere.chmod(0o600)
    store.path.symlink_to(elsewhere)
    with pytest.raises(KeyringUnreadableError):
        store.load()


@pytest.mark.req("ENF-SEC-005")
def test_review_keyring_owned_by_another_uid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = store_in(tmp_path)
    store.create(Keyring.generate(NOW))
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(store.path).st_uid + 1)
    with pytest.raises(KeyringPermissionsError):
        store.load()


@pytest.mark.req("ENF-SEC-005")
def test_review_lock_file_symlink_is_not_followed(tmp_path: Path) -> None:
    store = store_in(tmp_path)
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    store.lock_path.symlink_to(target)
    with pytest.raises(OSError), store.lock():
        pass
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.req("ENF-SEC-005")
def test_review_database_that_cannot_be_inspected_refuses_the_start(tmp_path: Path) -> None:
    import sqlite3

    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    database = sqlite3.connect(settings.database_path)
    database.execute("CREATE TABLE secret_key (kid TEXT PRIMARY KEY)")
    database.execute("INSERT INTO secret_key VALUES ('k-old')")
    database.commit()
    writer = sqlite3.connect(settings.database_path, isolation_level=None, timeout=0)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(KeyringMissingError):
            diagnostics.boot_check(settings)
        assert not (settings.data_dir / "keyring").exists()
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        database.close()


@pytest.mark.req("ENF-SEC-005")
def test_review_unmigrated_database_is_not_an_obstacle_to_a_first_start(tmp_path: Path) -> None:
    import sqlite3

    settings = data_settings(tmp_path)
    settings.data_dir.mkdir()
    sqlite3.connect(settings.database_path).close()  # exists, no table at all
    assert diagnostics.boot_check(settings).active.state == "active"


def external_store(tmp_path: Path) -> tuple[KeyringStore, Path]:
    real = tmp_path / "run" / "secrets" / "keyring-real"
    real.parent.mkdir(parents=True)
    real.write_bytes(Keyring.generate(NOW).to_json())
    link = tmp_path / "run" / "keyring"
    link.symlink_to(real)
    return KeyringStore(link, lock_path=tmp_path / "lock", external=True), real


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("mode", [0o400, 0o440, 0o444, 0o600, 0o644])
def test_external_keyring_may_be_a_symlink_and_read_only_or_world_readable(
    tmp_path: Path, mode: int
) -> None:
    store, real = external_store(tmp_path)
    real.chmod(mode)
    assert store.load().active.state == "active"


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666, 0o660])
def test_external_keyring_modifiable_by_group_or_others_is_refused(
    tmp_path: Path, mode: int
) -> None:
    store, real = external_store(tmp_path)
    real.chmod(mode)
    with pytest.raises(KeyringPermissionsError):
        store.load()


class _OwnedByRoot:
    """A real ``stat_result`` seen as owned by root; every other field is the real one."""

    def __init__(self, real: os.stat_result) -> None:
        self._real = real

    st_uid = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


@pytest.mark.req("ENF-SEC-005")
def test_external_keyring_owned_by_a_stranger_is_refused_but_root_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, real = external_store(tmp_path)
    real.chmod(0o400)
    owner = real.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    if owner != 0:
        with pytest.raises(KeyringPermissionsError):
            store.load()
    real_stat = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: _OwnedByRoot(real_stat(fd)))
    assert store.load().active.state == "active"  # owned by root


@pytest.mark.req("ENF-SEC-005")
def test_status_warns_for_a_world_readable_external_keyring(tmp_path: Path) -> None:
    store, real = external_store(tmp_path)
    settings = data_settings(tmp_path, SHELFHOLD_SECRET_KEY_FILE=str(store.path))
    settings.data_dir.mkdir()
    real.chmod(0o644)
    assert "keyring_world_readable" in {w.code for w in diagnostics.collect_warnings(settings)}
    real.chmod(0o400)
    assert "keyring_world_readable" not in {w.code for w in diagnostics.collect_warnings(settings)}
