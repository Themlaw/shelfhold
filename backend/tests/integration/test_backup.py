"""EF-INT-015, ENF-OPS-001, EF-INT-027 (M1b L23): the integrated backup, run and verify.

The cryptography is age (scrypt) from `pyrage`, whose work factor is deliberate: a run costs a
second or two, so the retention is played with a short one and the default is read from the
settings.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import decrypt_export
from shelfhold.jobs.maintenance import MaintenanceBusyError
from shelfhold.ops.backup import (
    BACKUP_FAILURES,
    BackupArchiveError,
    BackupError,
    backup_warnings,
)
from shelfhold.ops.backup import (
    run as backup_run_module,
)
from shelfhold.ops.backup.archive import KEYRING_MEMBER
from shelfhold.ops.backup.run import verify_latest
from tests.backup_env import MARKER, PASSPHRASE, backup_bench, write_passphrase

pytestmark = pytest.mark.req("EF-INT-015", "ENF-OPS-001")


def tree(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


def every_byte(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())


def test_backup_to_distinct_destination_with_retention(tmp_path: Path) -> None:
    assert Settings().backup_retention == 14  # EF-INT-015: 14 daily backups by default
    with backup_bench(tmp_path, backup_retention=3) as bench:
        written = []
        for _ in range(5):
            bench.clock.advance(days=1)
            written.append(bench.service.run().name)
        assert bench.names() == written[-3:]  # the two oldest left, one at a time, after a new one
        data = bench.settings.data_dir.resolve()
        assert data not in bench.backups.resolve().parents and bench.backups != data
        mode = stat.S_IMODE(os.stat(bench.backups / written[-1]).st_mode)
        assert mode == 0o600
        assert not [n for n in os.listdir(bench.backups) if n.startswith(".")]  # no scratch left
        (last,) = [r for r in bench.engine.connect().execute(sa.select(platform.backup_run))][-1:]
        assert last.state == "ok" and last.file_name == written[-1] and last.size > 0


@pytest.mark.parametrize("where", ["data", "store", "inside_data", "around_data"])
def test_the_destination_must_be_apart_from_data_and_store(tmp_path: Path, where: str) -> None:
    with backup_bench(tmp_path) as bench:
        target = {
            "data": bench.settings.data_dir,
            "store": bench.settings.store_dir,
            "inside_data": bench.settings.data_dir / "backups",
            "around_data": tmp_path,
        }[where]
        service = bench.service_for(bench.settings.model_copy(update={"backup_dir": target}))
        with pytest.raises(BackupError) as refused:
            service.run()
        assert refused.value.reason == "destination_not_distinct"
        assert not (bench.settings.data_dir / "backups").exists()


def test_the_oldest_backup_stays_unless_the_new_one_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with backup_bench(tmp_path, backup_retention=2) as bench:
        for _ in range(2):
            bench.clock.advance(days=1)
            bench.service.run()
        before = bench.names()

        def refuse(*_: object) -> None:
            raise BackupArchiveError

        monkeypatch.setattr(backup_run_module, "check_archive", refuse)
        bench.clock.advance(days=1)
        with pytest.raises(BackupError) as refused:
            bench.service.run()
        assert refused.value.reason == "verify_failed"
        assert bench.names() == before  # nothing pruned, and the unverified file is gone
        assert [n for n in os.listdir(bench.backups) if n.startswith(".")] == []


def test_backup_failure_notifies(tmp_path: Path) -> None:
    with backup_bench(tmp_path, backup_dir=tmp_path / "missing" / "backups") as bench:
        with pytest.raises(BackupError) as refused:
            bench.service.run()
        assert refused.value.reason in BACKUP_FAILURES
        assert refused.value.reason == "destination_unavailable"
        assert bench.events("backup.failed") == [{"reason": "destination_unavailable"}]
        with bench.engine.connect() as connection:
            row = connection.execute(sa.select(platform.backup_run)).one()
        assert (row.state, row.cause) == ("failed", "destination_unavailable")
        # `backup.stale` once per episode: nothing good was ever made
        assert len(bench.events("backup.stale")) == 1
        with pytest.raises(BackupError):
            bench.service.run()
        assert len(bench.events("backup.failed")) == 2 and len(bench.events("backup.stale")) == 1


def test_backup_stale_follows_a_good_backup_that_is_too_old(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.service.run()
        assert not bench.service.stale().stale
        bench.clock.advance(hours=bench.settings.backup_stale_hours + 1)
        assert bench.service.stale().stale
        assert bench.service.announce_stale_if_due()
        assert not bench.service.announce_stale_if_due()  # once
        assert bench.events("backup.stale") == [{"hours": 36}]
        bench.service.run()  # a good one ends the episode
        bench.clock.advance(hours=bench.settings.backup_stale_hours + 1)
        assert bench.service.announce_stale_if_due()
        assert len(bench.events("backup.stale")) == 2


def test_backup_without_passphrase_fails_never_plaintext(tmp_path: Path) -> None:
    with backup_bench(tmp_path, backup_passphrase_file=None) as bench:
        before = tree(tmp_path)
        with pytest.raises(BackupError) as refused:
            bench.service.run()
        assert refused.value.reason == "passphrase_missing"
        assert bench.events("backup.failed") == [{"reason": "passphrase_missing"}]
        assert tree(tmp_path) == before  # not a byte written: no file, no temporary, no folder
        assert not bench.backups.exists()
    # a successful run never leaves the data readable either: not in an archive, not beside it
    with backup_bench(tmp_path / "ok") as bench:
        bench.service.run()
        raw = every_byte(bench.backups)
        assert MARKER.encode() not in raw and b"SQLite format 3" not in raw
        assert b"schema_version" not in raw
        assert [n for n in os.listdir(bench.backups) if n.startswith(".")] == []


def test_a_failure_in_the_middle_leaves_no_clear_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with backup_bench(tmp_path) as bench:

        def boom(*_: object) -> bytes:
            raise RuntimeError("the cipher failed")

        monkeypatch.setattr(backup_run_module, "encrypt_archive", boom)
        with pytest.raises(RuntimeError):
            bench.service.run()
        assert os.listdir(bench.backups) == []  # the 0700 work folder with the copy is gone
        assert bench.events("backup.failed") == [{"reason": "write_failed"}]


def test_a_killed_run_leaves_a_work_folder_that_the_next_run_sweeps(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.backups.mkdir(mode=0o700)
        leftover = bench.backups / ".work-abcd1234"
        leftover.mkdir(mode=0o700)
        (leftover / "snapshot.db").write_bytes(b"SQLite format 3\x00 clear")
        stale = bench.clock.now().timestamp() - 7200
        os.utime(leftover, (stale, stale))
        bench.service.run()
        assert not leftover.exists()


def test_backup_refuses_passphrase_inside_backed_up_tree(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        inside = write_passphrase(bench.settings.data_dir / "backup-passphrase")
        settings = bench.settings.model_copy(update={"backup_passphrase_file": inside})
        service = bench.service_for(settings)
        with pytest.raises(BackupError) as refused:
            service.run()
        assert refused.value.reason == "passphrase_colocated"
        assert not bench.backups.exists() or os.listdir(bench.backups) == []
        # beside the archives is as bad
        beside = write_passphrase(bench.backups / "passphrase")
        with pytest.raises(BackupError) as again:
            bench.service_for(
                bench.settings.model_copy(update={"backup_passphrase_file": beside})
            ).run()
        assert again.value.reason == "passphrase_colocated"
        # a folder that is a link into `/data` does not hide it
        link = tmp_path / "innocent"
        link.symlink_to(bench.settings.data_dir)
        with pytest.raises(BackupError) as linked:
            bench.service_for(
                bench.settings.model_copy(
                    update={"backup_passphrase_file": link / "backup-passphrase"}
                )
            ).run()
        assert linked.value.reason == "passphrase_colocated"
        # the owner's choice: accepted, journaled, and a durable warning for `status`
        report = service.run(accept_colocated=True)
        assert report.name in bench.names()
        codes = {w["code"] for w in backup_warnings(bench.engine, settings, bench.clock.now())}
        assert "backup_passphrase_colocated" in codes
        # the next run with a safe file lifts the warning
        bench.clock.advance(days=1)
        bench.service.run()
        codes = {
            w["code"] for w in backup_warnings(bench.engine, bench.settings, bench.clock.now())
        }
        assert "backup_passphrase_colocated" not in codes


def test_the_passphrase_file_is_the_one_of_secrets_export(tmp_path: Path) -> None:
    """The file is vetted by `read_passphrase_file`: a link, a loose mode or a short phrase is
    refused as `passphrase_invalid`, whatever the backup would have done next."""
    with backup_bench(tmp_path) as bench:
        loose = write_passphrase(tmp_path / "loose")
        loose.chmod(0o644)
        short = write_passphrase(tmp_path / "short", "tooshort")
        target = write_passphrase(tmp_path / "target")
        link = tmp_path / "link"
        link.symlink_to(target)
        for path in (loose, short, link):
            service = bench.service_for(
                bench.settings.model_copy(update={"backup_passphrase_file": path})
            )
            with pytest.raises(BackupError) as refused:
                service.run()
            assert refused.value.reason == "passphrase_invalid"
        assert not bench.backups.exists() or os.listdir(bench.backups) == []


def test_master_key_export_encrypted_by_passphrase(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.service.run()
        _name, checked = verify_latest(bench.settings)
        armored = checked.members[KEYRING_MEMBER].decode("ascii")
        assert armored.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
        keyring = diagnostics.store_for(bench.settings).load()
        assert decrypt_export(armored, PASSPHRASE).keys == keyring.keys
        with pytest.raises(ExportError):
            decrypt_export(armored, "not the passphrase at all 1")
        # the key material is in no member and no file in the clear, in neither base64 form
        for key in keyring.keys:
            for form in (base64.b64encode(key.material), base64.urlsafe_b64encode(key.material)):
                assert form not in every_byte(bench.backups)
                assert all(form not in data for data in checked.members.values())


def test_vacuum_into_requires_twice_db_size_free(tmp_path: Path) -> None:
    calls: list[tuple[int, str]] = []

    class Room:
        def __init__(self, answer: bool) -> None:
            self.answer = answer

        def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool:
            calls.append((database_bytes, volume))
            return self.answer

    with backup_bench(tmp_path) as bench:
        size = sum(
            p.stat().st_size
            for p in (bench.settings.database_path, Path(f"{bench.settings.database_path}-wal"))
            if p.exists()
        )
        with pytest.raises(BackupError) as refused:
            bench.service_for(bench.settings, space=Room(False)).run()
        assert refused.value.reason == "no_room"
        assert [volume for _, volume in calls] == ["backup"] and calls[0][0] >= size
        assert os.listdir(bench.backups) == []
        assert bench.events("backup.failed") == [{"reason": "no_room"}]
        bench.service_for(bench.settings, space=Room(True)).run()  # enough room: it goes ahead
        assert len(bench.names()) == 1


def test_the_default_space_check_is_twice_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Usage:
        f_frsize = 4096

        def __init__(self, blocks: int) -> None:
            self.f_bavail = blocks

    disk = backup_run_module.DiskSpace(tmp_path)
    monkeypatch.setattr(os, "statvfs", lambda _p: Usage(2000))
    assert disk.has_room_for_vacuum(database_bytes=1000 * 4096, volume="backup")
    assert not disk.has_room_for_vacuum(database_bytes=1001 * 4096, volume="backup")


def test_backup_set_contains_full_keyring_and_schema_assets(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        keyring = diagnostics.store_for(bench.settings).load()
        rotated = keyring.rotated(bench.clock.now())  # the retired key must travel too
        diagnostics.store_for(bench.settings).write(rotated)
        bench.service.run()
        name, checked = verify_latest(bench.settings)
        members = checked.members
        assert {
            "shelfhold.db", "config.toml", "keyring.age", "docs/schema.sql",
            "docs/data-dictionary.md", "meta/units/ab/01JTEST0000000000000000AB1.json",
        } <= set(members)  # fmt: skip
        assert [k.kid for k in checked.keyring.keys] == [k.kid for k in rotated.keys]
        assert len(checked.keyring.keys) == 2  # active and retired
        assert b"CREATE TABLE" in members["docs/schema.sql"]
        assert b"Shelfhold data dictionary" in members["docs/data-dictionary.md"]
        # the database is a consistent copy, read with `sqlite3` alone
        connection = sqlite3.connect(":memory:")
        connection.deserialize(members["shelfhold.db"])
        marker = connection.execute(
            "SELECT value FROM app_setting WHERE key = 'test.marker'"
        ).fetchone()
        assert marker == (MARKER,)
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        connection.close()
        assert name in bench.names()


def test_manifests_of_the_library_roots_come_only_with_a_manual_run(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        root = tmp_path / "library"
        (root / ".shelfhold" / "manifest").mkdir(parents=True)
        (root / ".shelfhold" / "manifest" / "m.json").write_text("{}", encoding="utf-8")
        (root / ".shelfhold" / "manifest" / "linked").symlink_to("/etc/hostname")
        with bench.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.physical_root).values(
                    root_id="01J00000000000000000000R01", path_entry=str(root)
                )
            )
        bench.service.run()  # scheduled: no library disk is read
        assert not [
            m for m in verify_latest(bench.settings)[1].members if m.startswith("manifests")
        ]
        bench.clock.advance(days=1)
        bench.service.run(manifests=True)
        names = [m for m in verify_latest(bench.settings)[1].members if m.startswith("manifests")]
        assert names == ["manifests/000/m.json"]  # the link is skipped, never followed


def test_verify_reports_a_damaged_backup_in_one_sentence(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.service.run()
        archive = bench.backups / bench.names()[0]
        archive.write_bytes(archive.read_bytes()[:-10])
        with pytest.raises(BackupArchiveError):
            verify_latest(bench.settings)


def test_a_vacuum_in_progress_makes_the_backup_wait_and_refuses_it_after(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        started = threading.Event()
        release = threading.Event()

        def vacuum() -> None:
            with bench.gate.exclusive() as ours:
                assert ours
                started.set()
                release.wait(5)

        thread = threading.Thread(target=vacuum)
        thread.start()
        assert started.wait(5)
        with pytest.raises(MaintenanceBusyError), bench.gate.hold("backup", wait_s=0.1):
            pass
        release.set()
        thread.join()
        with bench.gate.hold("backup"), bench.gate.exclusive() as ours:
            assert not ours  # and while a backup holds, no VACUUM starts


def test_the_gate_reaches_another_process_through_its_lock_file(tmp_path: Path) -> None:
    """`shelfhold backup run` is a separate process: it holds the gate by `flock`, so the VACUUM
    of the server cannot start under it, and it waits for one that is running."""
    from shelfhold.jobs.maintenance import MaintenanceGate

    lock = tmp_path / ".maintenance.lock"
    server, cli = MaintenanceGate(lock), MaintenanceGate(lock)
    with cli.hold("backup"), server.exclusive() as ours:
        assert not ours
    with server.exclusive() as ours:
        assert ours
        with pytest.raises(MaintenanceBusyError), cli.hold("backup", wait_s=0.3):
            pass
    with cli.hold("backup"):  # free again
        pass
