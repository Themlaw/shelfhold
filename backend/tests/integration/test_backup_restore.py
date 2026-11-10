"""EF-INT-015, EF-INT-026, EF-INT-027 (M1b L23): restore on another path, the restored instance
started behind its disk, `reconcile --adopt-after-restore`, and a `/store` that is not there.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog
from shelfhold.infrastructure.secrets.keyring import KeyringStore
from shelfhold.ops.backup import (
    BackupService,
    DestinationError,
    mark_store_missing,
    restore_archive,
)
from tests.backup_env import MARKER, backup_bench, write_passphrase
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-INT-015", "EF-INT-026", "EF-INT-027")


def digest(folder: Path) -> dict[str, str]:
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def test_restore_on_other_path(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.service.run()
        archive = bench.backups / bench.names()[0]
        live = digest(bench.settings.data_dir)
        target = tmp_path / "restored"
        report = restore_archive(
            archive, target, bench.passphrase, live_data_dir=bench.settings.data_dir
        )
        assert report.destination == target.resolve()
        # the tree a new /data starts from: database, keyring (owner-only), config, meta, docs
        assert (target / "keyring").stat().st_mode & 0o777 == 0o600
        assert (target / "config.toml").read_bytes() == (
            bench.settings.data_dir / "config.toml"
        ).read_bytes()
        assert (target / "meta/units/ab/01JTEST0000000000000000AB1.json").is_file()
        assert (target / "docs/schema.sql").is_file()
        restored = sqlite3.connect(target / "shelfhold.db")
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert restored.execute(
            "SELECT value FROM app_setting WHERE key = 'test.marker'"
        ).fetchone() == (MARKER,)
        restored.close()
        keyring = KeyringStore(target / "keyring", lock_path=target / ".lock").load()
        assert [k.kid for k in keyring.keys] == [
            k.kid for k in KeyringStore(
                bench.settings.data_dir / "keyring", lock_path=target / ".lock"
            ).load().keys
        ]  # fmt: skip
        assert digest(bench.settings.data_dir) == live  # the live /data is untouched


def test_restore_refuses_the_live_data_dir_a_full_folder_and_a_file(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.service.run()
        archive = bench.backups / bench.names()[0]
        live = digest(bench.settings.data_dir)
        full = tmp_path / "full"
        full.mkdir()
        (full / "x").write_text("x")
        a_file = tmp_path / "a-file"
        a_file.write_text("x")
        cases = {
            "is_live_data": [bench.settings.data_dir, bench.settings.data_dir / "sub"],
            "not_empty": [full, a_file],
        }
        for reason, targets in cases.items():
            for target in targets:
                with pytest.raises(DestinationError) as refused:
                    restore_archive(
                        archive, target, bench.passphrase, live_data_dir=bench.settings.data_dir
                    )
                assert refused.value.reason == reason
        assert (
            digest(bench.settings.data_dir) == live
            and not (bench.settings.data_dir / "sub").exists()
        )
        wrong = write_passphrase(tmp_path / "other", "a different synthetic passphrase")
        from shelfhold.ops.backup import BackupArchiveError

        with pytest.raises(BackupArchiveError):
            restore_archive(archive, tmp_path / "never", wrong)
        assert not (tmp_path / "never").exists()


def stored_copy(env: RestoreEnv, number: str, store: Path, *, present: bool) -> str:
    """The Unit is `stored`: its kept copy is `path` under `/store`, there or not."""
    unit = env.chapters[number]
    path = f"keep/{number}.cbz"
    with env.engine.begin() as connection:
        connection.execute(sa.text("UPDATE unit SET state = 'stored' WHERE id = :u"), {"u": unit})
        connection.execute(
            sa.text("UPDATE content SET retention = 'keep', path = :p WHERE unit_id = :u"),
            {"u": unit, "p": path},
        )
    if present:
        (store / "keep").mkdir(parents=True, exist_ok=True)
        (store / path).write_bytes(b"kept copy " + number.encode())
    return unit


def unit_states(env: RestoreEnv) -> dict[str, str]:
    with env.engine.connect() as connection:
        rows = connection.execute(sa.select(catalog.unit.c.id, catalog.unit.c.state))
        return {r.id: r.state for r in rows}


def test_restore_without_store_marks_store_missing_no_deletion(tmp_path: Path) -> None:
    """The whole way: backup, more is filed on the disk, restore on another path, the instance
    starts `restored_behind`, `reconcile --adopt-after-restore` re-adopts, and the Unit whose kept
    copy is not under `/store` waits as `store_missing`. Not one file is deleted anywhere."""
    store = tmp_path / "store"
    with restore_env() as env:
        data = env.db_path.parent  # where the instance's database is: its /data
        kept = stored_copy(env, "2", store, present=True)
        lost = stored_copy(env, "1", store, present=False)
        settings = Settings(
            data_dir=data, store_dir=store, backup_dir=tmp_path / "backups",
            secret_key_file=env.ex.lib.data / "keyring",
            backup_passphrase_file=write_passphrase(tmp_path / "secrets" / "p"),
        )  # fmt: skip
        with env.engine.begin() as connection:  # the fixture builds the schema without Alembic
            connection.execute(
                sa.text("CREATE TABLE IF NOT EXISTS alembic_version (version_num TEXT)")
            )
            connection.execute(sa.text("DELETE FROM alembic_version"))
            connection.execute(sa.text("INSERT INTO alembic_version VALUES ('0016')"))
        service = BackupService(settings, env.engine, clock=env.ex.clock, bus=env.bus, gate=_Gate())
        env.add_chapters(3, 5)  # known to the backup, not yet exported
        service.run()
        env.export()  # the disk moves on after the backup
        archive = settings.backup_dir / sorted(p.name for p in settings.backup_dir.iterdir())[0]  # type: ignore[operator]
        target = tmp_path / "restored"
        restore_archive(archive, target, settings.backup_passphrase_file)  # type: ignore[arg-type]
        env.restore(target / "shelfhold.db")  # the new instance runs on the restored database

        status = env.guard().enter_restored_behind()
        assert status.behind  # started in restored_behind, nothing written until the adoption
        assert len(env.events("instance.restored_behind")) == 1
        library = env.files()
        adopter = env.adopter()
        result = adopter.adopt(confirm=adopter.preview().fingerprint)
        assert len(result.readopted) == 3 and not result.deleted

        before = digest(store)
        assert mark_store_missing(env.engine, store, env.bus) == 1
        assert unit_states(env)[lost] == "store_missing"
        assert unit_states(env)[kept] == "stored"
        assert digest(store) == before  # nothing in /store touched
        assert set(library) <= set(env.files())  # nothing of the library gone
        assert env.events("reconcile.drift")[-1] == {"kind": "store_missing", "count": 1}
        assert mark_store_missing(env.engine, store, env.bus) == 0  # a second look changes nothing

        # and with no /store at all every `stored` Unit waits, and none is deleted
        stored_copy(env, "2", store, present=False)
        assert mark_store_missing(env.engine, tmp_path / "no-such-store", env.bus) == 1
        assert unit_states(env)[kept] == "store_missing"
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM unit")).scalar() == 5


class _Gate:
    """A gate that never waits: the restore tests have no VACUUM."""

    def hold(self, name: str, *, wait_s: float = 0.0):  # type: ignore[no-untyped-def]
        import contextlib

        return contextlib.nullcontext()
