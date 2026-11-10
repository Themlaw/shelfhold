"""EF-INT-015, ENF-OPS-001, EF-INT-027 (M1b L23): the integrated backup, run and verify.

The cryptography is age (scrypt) from `pyrage`, whose work factor is deliberate: a run costs a
second or two, so the retention is played with a short one and the default is read from the
settings.
"""

from __future__ import annotations

import base64
import errno
import os
import signal
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
from shelfhold.ops.backup import archive as arc
from shelfhold.ops.backup import (
    run as backup_run_module,
)
from shelfhold.ops.backup import store as store_module
from shelfhold.ops.backup.archive import KEYRING_MEMBER
from shelfhold.ops.backup.run import verify_latest
from shelfhold.ops.backup.scratch import BackupLock, scratch_folder, sweep_orphans
from shelfhold.ops.backup.store import BackupDirectory
from tests.backup_env import MARKER, PASSPHRASE, backup_bench, read_members, write_passphrase

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

        monkeypatch.setattr(backup_run_module, "check_file", refuse)
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

        def boom(*_: object) -> None:
            raise RuntimeError("the cipher failed")

        monkeypatch.setattr(backup_run_module, "write_archive", boom)
        with pytest.raises(RuntimeError):
            bench.service.run()
        assert os.listdir(bench.backups) == []  # no partial file, and no clear copy anywhere
        assert not scratch_folders(bench)
        assert bench.events("backup.failed") == [{"reason": "write_failed"}]


def scratch_folders(bench: object) -> list[Path]:
    data = bench.settings.data_dir  # type: ignore[attr-defined]
    return sorted((data / "tmp").glob("backup-*")) if (data / "tmp").is_dir() else []


def test_a_killed_run_leaves_nothing_on_the_destination_and_the_next_run_sweeps_the_snapshot(
    tmp_path: Path,
) -> None:
    """The clear snapshot is in `/data/tmp`, never in the (synchronised, perhaps FAT) backup
    folder: a SIGKILL right after `VACUUM INTO` leaves the destination with no byte at all."""
    with backup_bench(tmp_path) as bench:
        original = bench.service._snapshot

        def killed(copy: Path) -> str:
            original(copy)
            os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError("not reached")

        pid = os.fork()
        if pid == 0:  # the child is the run that dies
            try:
                bench.engine.dispose(close=False)
                bench.service._snapshot = killed  # type: ignore[method-assign]
                bench.service.run()
            finally:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        assert tree(bench.backups) == [] if bench.backups.exists() else True
        left = scratch_folders(bench)
        assert len(left) == 1 and (left[0] / "snapshot.db").is_file()
        assert stat.S_IMODE(left[0].stat().st_mode) == 0o700
        # nobody holds it any more: the next run (and the start-up) remove it
        assert sweep_orphans(bench.settings.data_dir) == 1 and not scratch_folders(bench)


def test_a_scratch_folder_whose_owner_is_alive_is_not_swept(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench, scratch_folder(bench.settings.data_dir) as folder:
        (folder / "snapshot.db").write_bytes(b"SQLite format 3\x00 clear")
        assert sweep_orphans(bench.settings.data_dir) == 0 and folder.is_dir()
    assert not folder.exists()


def test_nothing_in_clear_reaches_the_destination_even_when_a_run_fails_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with backup_bench(tmp_path) as bench:
        seen: list[bytes] = []
        real = backup_run_module.write_archive

        def watching(sink: object, chunks: object, passphrase: str) -> None:
            real(sink, chunks, passphrase)  # type: ignore[arg-type]
            seen.append(every_byte(bench.backups))
            raise OSError("the disk went away")

        monkeypatch.setattr(backup_run_module, "write_archive", watching)
        with pytest.raises(BackupError):
            bench.service.run()
        assert seen and all(
            MARKER.encode() not in raw and b"SQLite format 3" not in raw for raw in seen
        )
        assert os.listdir(bench.backups) == []


def test_only_known_archives_are_ever_removed(tmp_path: Path) -> None:
    """Retention takes the archives this instance recorded as written and verified, and that
    start like one of ours: a foreign file or folder with an archive-like name stays."""
    with backup_bench(tmp_path, backup_retention=2) as bench:
        bench.service.run()
        bench.backups.joinpath("shelfhold-backup-20200101T000000Z.tar.age").write_bytes(b"mine?")
        (bench.backups / "shelfhold-backup-20200102T000000Z.tar.age").mkdir()
        (bench.backups / ".work-abcd1234").mkdir()
        (bench.backups / ".work-abcd1234" / "precious").write_text("x")
        (bench.backups / ".partial-0123456789abcdef").write_bytes(b"not an archive start")
        old = bench.clock.now().timestamp() - 86400
        os.utime(bench.backups / ".partial-0123456789abcdef", (old, old))
        for _ in range(3):
            bench.clock.advance(days=1)
            bench.service.run()
        kept = os.listdir(bench.backups)
        assert "shelfhold-backup-20200101T000000Z.tar.age" in kept  # unrecorded
        assert "shelfhold-backup-20200102T000000Z.tar.age" in kept  # a folder
        assert (bench.backups / ".work-abcd1234" / "precious").read_text() == "x"
        assert ".partial-0123456789abcdef" in kept  # does not start like an archive
        real = [n for n in kept if n.startswith("shelfhold-backup-2026")]
        assert len(real) == 2  # the retention counted our own


def test_a_dead_partial_of_ours_is_swept(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.backups.mkdir(mode=0o700)
        partial = bench.backups / ".partial-0123456789abcdef"
        partial.write_bytes(arc.ARCHIVE_MAGIC + b"12\nhalf an archive")
        old = bench.clock.now().timestamp() - 7200
        os.utime(partial, (old, old))
        bench.service.run()
        assert not partial.exists()


def test_two_backups_do_not_run_together(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench, BackupLock(bench.settings.data_dir):
        with pytest.raises(BackupError) as refused:
            bench.service.run()
        assert refused.value.reason == "locked"  # not "busy": that one is a VACUUM
        assert not bench.backups.exists() or not bench.names()


def test_a_run_found_running_with_nobody_holding_the_lock_is_interrupted(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        with bench.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.backup_run).values(
                    id="01J00000000000000000000B01", started_at=bench.clock.now(), state="running"
                )
            )
        assert bench.service.settle_interrupted() == 1
        assert bench.events("backup.failed") == [{"reason": "interrupted"}]
        with bench.engine.connect() as connection:
            row = connection.execute(sa.select(platform.backup_run)).one()
        assert (row.state, row.cause) == ("failed", "interrupted") and row.finished_at is not None
        assert bench.service.settle_interrupted() == 0
        # a backup that is really running is left alone
        with bench.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.backup_run).values(
                    id="01J00000000000000000000B02", started_at=bench.clock.now(), state="running"
                )
            )
        with BackupLock(bench.settings.data_dir):
            assert bench.service.settle_interrupted() == 0


def test_the_archive_is_placed_without_renameat2_on_a_file_system_that_lacks_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAT, exFAT, CIFS: `renameat2(RENAME_NOREPLACE)` answers EPERM, EINVAL or ENOSYS; the
    archive still lands, an existing one is still never replaced, and no hard link is tried."""
    for code in (errno.EPERM, errno.EINVAL, errno.ENOSYS):

        def refused(*_a: object, _code: int = code) -> None:
            raise OSError(_code, os.strerror(_code))

        def no_link(*_a: object, **_k: object) -> None:
            raise AssertionError("a hard link on a file system that may not have them")

        with backup_bench(tmp_path / str(code)) as bench, monkeypatch.context() as patch:
            patch.setattr(store_module, "renameat2_noreplace", refused)
            patch.setattr(os, "link", no_link)
            report = bench.service.run()
            assert bench.names() == [report.name]
            assert os.listdir(bench.backups) == [report.name]
            directory = BackupDirectory.open(bench.backups)
            with pytest.raises(FileExistsError):
                directory.write(report.name, lambda sink: sink.write(arc.ARCHIVE_MAGIC + b"1\n"))
            assert verify_latest(bench.settings)[0] == report.name  # the first is intact
            assert os.listdir(bench.backups) == [report.name]  # and the refusal left no partial


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
        name, _checked = verify_latest(bench.settings)
        members = read_members(bench.backups / name)
        armored = members[KEYRING_MEMBER].decode("ascii")
        assert armored.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
        keyring = diagnostics.store_for(bench.settings).load()
        assert decrypt_export(armored, PASSPHRASE).keys == keyring.keys
        with pytest.raises(ExportError):
            decrypt_export(armored, "not the passphrase at all 1")
        # the key material is in no member and no file in the clear, in neither base64 form
        for key in keyring.keys:
            for form in (base64.b64encode(key.material), base64.urlsafe_b64encode(key.material)):
                assert form not in every_byte(bench.backups)
                assert all(form not in data for data in members.values())


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
        assert [volume for _, volume in calls] == ["data"] and calls[0][0] >= size
        assert not bench.backups.exists() or os.listdir(bench.backups) == []
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
    assert disk.has_room_for_vacuum(database_bytes=1000 * 4096, volume="data")
    assert not disk.has_room_for_vacuum(database_bytes=1001 * 4096, volume="data")


def test_backup_set_contains_full_keyring_and_schema_assets(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        keyring = diagnostics.store_for(bench.settings).load()
        rotated = keyring.rotated(bench.clock.now())  # the retired key must travel too
        diagnostics.store_for(bench.settings).write(rotated)
        bench.service.run()
        name, checked = verify_latest(bench.settings)
        members = read_members(bench.backups / name)
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


def test_a_hard_link_into_data_is_the_same_file_and_so_colocated(tmp_path: Path) -> None:
    """The path of the passphrase file is outside `/data`, but it is the same inode as a file in
    it (a hard link, as a bind mount would be): the identity of the open file gives it away."""
    with backup_bench(tmp_path) as bench:
        inside = write_passphrase(bench.settings.data_dir / "somewhere" / "p")
        outside = tmp_path / "elsewhere" / "p"
        outside.parent.mkdir()
        os.link(inside, outside)
        service = bench.service_for(
            bench.settings.model_copy(update={"backup_passphrase_file": outside})
        )
        with pytest.raises(BackupError) as refused:
            service.run()
        assert refused.value.reason == "passphrase_colocated"
        assert not bench.backups.exists() or not bench.names()
        service.run(accept_colocated=True)  # the owner's choice, flagged for `status`
        codes = {
            w["code"] for w in backup_warnings(bench.engine, bench.settings, bench.clock.now())
        }
        assert "backup_passphrase_colocated" in codes


def test_a_retention_above_a_month_is_refused_by_the_settings() -> None:
    from pydantic import ValidationError

    assert Settings(backup_retention=30).backup_retention == 30
    with pytest.raises(ValidationError):
        Settings(backup_retention=31)


def test_the_lock_is_taken_before_the_run_row_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `running` row with nobody holding the lock is a dead run: so no row may exist before the
    lock is ours (a run waiting for the lock must not be taken for a dead one by the holder)."""
    from shelfhold.ops.backup.scratch import LockBusyError

    with backup_bench(tmp_path) as bench:
        real = type(bench.service)._begin
        seen: list[bool] = []

        def begin(self: object) -> str:
            try:
                with BackupLock(bench.settings.data_dir):
                    seen.append(False)  # we could take the lock: the run does not hold it
            except LockBusyError:
                seen.append(True)
            return real(self)  # type: ignore[arg-type]

        monkeypatch.setattr(type(bench.service), "_begin", begin)
        bench.service.run()
        assert seen == [True]


def test_a_live_run_is_never_marked_interrupted_by_another_process(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        with bench.engine.begin() as connection:  # the CLI run that is going on
            connection.execute(
                sa.insert(platform.backup_run).values(
                    id="01J00000000000000000000B03", started_at=bench.clock.now(), state="running"
                )
            )
        with BackupLock(bench.settings.data_dir):  # ... and holds the lock
            with pytest.raises(BackupError) as refused:  # the scheduled run meets it
                bench.service.run()
            assert refused.value.reason == "locked"
            assert bench.service.settle_interrupted() == 0  # nor does the start-up
        with bench.engine.connect() as connection:
            rows = {
                r.id: (r.state, r.cause) for r in connection.execute(sa.select(platform.backup_run))
            }
        assert rows["01J00000000000000000000B03"] == ("running", None)
        assert [c for s, c in rows.values() if s == "failed"] == ["locked"]
        assert bench.events("backup.failed") == [{"reason": "locked"}]


def test_a_hard_link_deep_in_the_destination_or_across_a_device_change_is_colocated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with backup_bench(tmp_path) as bench:
        inside = write_passphrase(bench.settings.data_dir / "a" / "p")
        outside = tmp_path / "elsewhere" / "p"
        outside.parent.mkdir()
        os.link(inside, outside)
        service = bench.service_for(
            bench.settings.model_copy(update={"backup_passphrase_file": outside})
        )
        # the folder of `/data` reports another device (a mount in the tree is another device):
        # the comparison is by entry, so it still finds the file
        real = os.lstat

        def lstat(path: object, *a: object, **k: object) -> os.stat_result:
            info = real(path, *a, **k)  # type: ignore[arg-type]
            if str(path) == str(bench.settings.data_dir):
                fields = list(info)
                fields[2] = info.st_dev + 1
                return os.stat_result(fields)
            return info

        monkeypatch.setattr(os, "lstat", lstat)
        with pytest.raises(BackupError) as refused:
            service.run()
        assert refused.value.reason == "passphrase_colocated"
        monkeypatch.undo()
        # and one deep in the backup folder, three levels down, is beside the archives too
        (bench.backups / "x" / "y" / "z").mkdir(parents=True)
        os.link(write_passphrase(tmp_path / "plain" / "p"), bench.backups / "x" / "y" / "z" / "p")
        deep = bench.service_for(
            bench.settings.model_copy(update={"backup_passphrase_file": tmp_path / "plain" / "p"})
        )
        with pytest.raises(BackupError) as again:
            deep.run()
        assert again.value.reason == "passphrase_colocated"


def test_a_run_without_manifests_never_reads_the_library_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENF-OPS-014: the scheduled run does not touch a library disk, not even to resolve a path
    for the passphrase check."""
    with backup_bench(tmp_path) as bench:

        def roots(_self: object) -> list[Path]:
            raise AssertionError("a library root was read by a run that copies no manifest")

        monkeypatch.setattr(type(bench.service), "_roots", roots)
        bench.service.run()  # no manifests: no root is read
        assert len(bench.names()) == 1
        bench.clock.advance(days=1)
        with pytest.raises(AssertionError):
            bench.service.run(manifests=True)  # the manual run with manifests does read them


def test_only_a_marked_scratch_folder_is_swept(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        data = bench.settings.data_dir
        (data / "tmp").mkdir(exist_ok=True)
        foreign = data / "tmp" / "backup-abcd1234"  # same name, someone else's, no marker
        foreign.mkdir()
        (foreign / "precious").write_text("x")
        ours = data / "tmp" / "backup-wxyz5678"
        ours.mkdir()
        (ours / ".shelfhold-backup-scratch").write_text("shelfhold backup scratch\n")
        (ours / "snapshot.db").write_bytes(b"clear")
        (ours / "meta").mkdir()
        (ours / "meta" / "00000000").write_bytes(b"clear")
        assert sweep_orphans(data) == 1
        assert not ours.exists()  # removed recursively
        assert (foreign / "precious").read_text() == "x"
        with scratch_folder(data) as live:
            assert (live / ".shelfhold-backup-scratch").is_file()


def test_an_empty_or_foreign_partial_name_is_not_swept(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        bench.backups.mkdir(mode=0o700)
        old = bench.clock.now().timestamp() - 7200
        for name, content in (
            (".partial-0123456789abcdef", b""),
            (".partial-fedcba9876543210", arc.ARCHIVE_MAGIC[:5]),
        ):
            (bench.backups / name).write_bytes(content)
            os.utime(bench.backups / name, (old, old))
        bench.service.run()
        assert {".partial-0123456789abcdef", ".partial-fedcba9876543210"} <= set(
            os.listdir(bench.backups)
        )


def test_a_work_factor_above_the_bound_is_retried_then_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """age sizes scrypt to a second of this machine; a seal above `MAX_SCRYPT_LOG_N` could never
    be read back. It is made again, and after three tries the run fails with its own cause."""
    from shelfhold.infrastructure.secrets import export as export_module

    real = export_module.check_scrypt_cost
    calls: list[int] = []

    def too_costly(binary: bytes) -> None:
        calls.append(1)
        raise ExportError("too costly")

    with backup_bench(tmp_path) as bench:
        monkeypatch.setattr(export_module, "check_scrypt_cost", too_costly)
        with pytest.raises(BackupError) as refused:
            bench.service.run()
        assert refused.value.reason == "kdf_cost" and len(calls) == 3  # three tries
        assert bench.events("backup.failed") == [{"reason": "kdf_cost"}]
        assert not bench.backups.exists() or os.listdir(bench.backups) == []
        # a calibration that lands above the bound once is simply made again
        attempts = iter([True, False, False, False])

        def noisy(binary: bytes) -> None:
            if next(attempts, False):
                raise ExportError("too costly")
            real(binary)

        monkeypatch.setattr(export_module, "check_scrypt_cost", noisy)
        bench.clock.advance(days=1)
        assert bench.service.run().name in bench.names()
