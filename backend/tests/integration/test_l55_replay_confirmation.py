"""L55 (EF-BIB-053 (3)): a deletion that the journal replay asks for after a restore needs the
admin to type the identifier of every irreplaceable Unit it would erase.

The tombstones are laid by the real `purge --now`; the restore is the real one (the database goes
back, the disk keeps what the purge left plus the files a disk backup brings back); the command is
the real `reconcile --adopt-after-restore`.
"""

import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings, load_settings
from shelfhold.export.domain.provenance_mirror import MIRROR_DIR
from shelfhold.infrastructure.schema import identity
from tests.auth_env import db_rows, run_cli
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-BIB-053")


def bring_back(env: RestoreEnv, settings: Settings, backup: Path, aside: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(settings.database_path) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup, settings.database_path)
    for source in aside.rglob("*"):  # the disk backup: exported files and their mirror lines
        relative = source.relative_to(aside)
        if source.is_file() and (relative.parts[0] != ".shelfhold" or MIRROR_DIR in str(relative)):
            target = env.library / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def fingerprint_of(out: str) -> str:
    return next(line.split(": ")[1].strip() for line in out.splitlines() if "Empreinte" in line)


def test_replay_deletion_of_irreplaceable_requires_typed_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with restore_env() as env:
        env.add_chapters(3, 4)
        replaceable, irreplaceable = env.chapters["3"], env.chapters["4"]
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE unit SET availability = 'removed' WHERE id = :u"),
                {"u": irreplaceable},
            )
        backup = env.backup()  # before the export: the database will be older than the disk
        env.export()
        lib = env.ex.lib
        settings = load_settings(
            {"SHELFHOLD_DATA_DIR": str(lib.data), "SHELFHOLD_STORE_DIR": str(lib.store)}
        ).values
        shutil.copyfile(env.backup("post-export.db"), settings.database_path)
        aside = lib.tmp / "disk-backup"
        shutil.copytree(env.library, aside, ignore=shutil.ignore_patterns(".staging", "lease"))

        for unit in (replaceable, irreplaceable):  # the real purge, from the host CLI
            assert run_cli(monkeypatch, settings, "purge", "--now", "--unit", unit,
                           "--confirm", unit) == 0  # fmt: skip

        def purged_files() -> list[Path]:
            return sorted((env.library / "manga" / "Kavita").rglob("*Ch.[34].cbz"))

        assert purged_files() == []  # erased by the purge
        bring_back(env, settings, backup, aside)
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "reconcile", "--adopt-after-restore") == 2
        out = capsys.readouterr().out
        fingerprint = fingerprint_of(out)
        assert irreplaceable in out and replaceable not in out  # the preview names what is at stake
        before = purged_files()
        assert len(before) == 2

        def adopt(*extra: str) -> int:
            return run_cli(monkeypatch, settings, "reconcile", "--adopt-after-restore",
                           "--confirm", fingerprint, "--confirm-deletion", *extra)  # fmt: skip

        # the reserved confirmation alone is not enough: nothing is deleted, nothing adopted
        assert adopt() == 2
        assert irreplaceable in capsys.readouterr().err
        assert purged_files() == before
        assert db_rows(settings, "SELECT restore_generation FROM instance")[0][0] > 0  # frozen
        # a wrong identifier, another Unit's identifier and a lower-case copy are no confirmation
        for wrong in (replaceable, irreplaceable.lower(), irreplaceable[:-1], " "):
            assert adopt("--confirm-unit", wrong) == 2, wrong
            capsys.readouterr()
            assert purged_files() == before
        with env.engine.connect() as connection:  # and no audit row: nothing was confirmed
            assert not [a for a in connection.execute(sa.select(identity.audit.c.action))
                        if a.action == "reconcile.confirm_deletion"]  # fmt: skip

        assert adopt("--confirm-unit", irreplaceable) == 0  # typed: both files go
        assert purged_files() == []
        assert db_rows(settings, "SELECT restore_generation FROM instance") == [(0,)]
        audit = db_rows(settings, "SELECT action FROM audit WHERE action LIKE 'reconcile.%'")
        assert [a[0] for a in audit] == ["reconcile.confirm_deletion"]
