"""L55 (EF-BIB-053 (3)): a deletion that the journal replay asks for after a restore needs the
admin to type the identifier of every irreplaceable Unit it would erase.

The tombstones are laid by the real `purge --now`; the restore is the real one (the database goes
back, the disk keeps what the purge left plus the files a disk backup brings back); the command is
the real `reconcile --adopt-after-restore`.
"""

import json
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings, load_settings
from shelfhold.export.domain.provenance_mirror import MIRROR_DIR
from shelfhold.infrastructure.schema import identity
from tests.auth_env import db_rows, run_cli
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-BIB-053")

Forge = Callable[[dict[str, Any], dict[str, str]], None]


@dataclass
class Scene:
    env: RestoreEnv
    settings: Settings
    backup: Path
    aside: Path
    replaceable: str
    irreplaceable: str

    def purged_files(self) -> list[Path]:
        return sorted((self.env.library / "manga" / "Kavita").rglob("*Ch.[34].cbz"))


def forge_mirror(aside: Path, forge: Forge, units: dict[str, str]) -> None:
    """Rewrite the coverage of every mirror line the way a hostile disk backup could."""
    for mirror in (aside / MIRROR_DIR).rglob("*.jsonl"):
        lines = []
        for raw in mirror.read_text().splitlines():
            entry = json.loads(raw)
            if isinstance(entry.get("coverage"), list):
                forge(entry, units)
            lines.append(json.dumps(entry))
        mirror.write_text("\n".join(lines) + "\n")


@contextmanager
def scene(
    monkeypatch: pytest.MonkeyPatch, forge: Forge | None = None
) -> Iterator[Scene]:
    """Two exported chapters (3 replaceable, 4 irreplaceable) purged by the real `purge --now`,
    then the database goes back and the disk backup (possibly forged) brings the files back."""
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
        if forge is not None:
            forge_mirror(aside, forge, {"replaceable": replaceable, "irreplaceable": irreplaceable})
        found = Scene(env, settings, backup, aside, replaceable, irreplaceable)
        assert found.purged_files() == []  # erased by the purge
        bring_back(env, settings, backup, aside)
        yield found


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


def adopt_with(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], sc: Scene, *extra: str
) -> int:
    """Preview, then the reserved confirmation of the preview's fingerprint, plus `extra`."""
    capsys.readouterr()
    assert run_cli(monkeypatch, sc.settings, "reconcile", "--adopt-after-restore") == 2
    fingerprint = fingerprint_of(capsys.readouterr().out)
    return run_cli(monkeypatch, sc.settings, "reconcile", "--adopt-after-restore",
                   "--confirm", fingerprint, "--confirm-deletion", *extra)  # fmt: skip


def drop_identifiers(entry: dict[str, Any], units: dict[str, str]) -> None:
    for covered in entry["coverage"]:  # no Variant, no Unit: the preview would name nothing
        for key in ("variant", "unit"):
            covered.pop(key, None)


def substitute_unit(entry: dict[str, Any], units: dict[str, str]) -> None:
    for covered in entry["coverage"]:  # the irreplaceable file claims the replaceable Unit
        covered.pop("variant", None)
        if covered.get("unit") == units["irreplaceable"]:
            covered["unit"] = units["replaceable"]


def drop_everything(entry: dict[str, Any], units: dict[str, str]) -> None:
    drop_identifiers(entry, units)
    for covered in entry["coverage"]:  # and the source's words: nothing in the database matches
        for key in ("source_key", "remote_unit_id"):
            covered.pop(key, None)


@pytest.mark.parametrize("forge", [drop_identifiers, substitute_unit])
def test_forged_mirror_does_not_hide_an_irreplaceable_unit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], forge: Forge
) -> None:
    """The Units to type come from the database, never from the untrusted mirror line."""
    with scene(monkeypatch, forge) as sc:
        before = sc.purged_files()
        assert len(before) == 2
        assert adopt_with(monkeypatch, capsys, sc) == 2  # the reserved action alone: refused
        assert sc.irreplaceable in capsys.readouterr().err
        assert sc.purged_files() == before
        assert adopt_with(monkeypatch, capsys, sc, "--confirm-unit", sc.irreplaceable) == 0
        assert sc.purged_files() == []


def test_file_no_known_unit_covers_requires_its_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tombstoned file the database cannot tie to any Unit is typed by its path, or kept."""
    with scene(monkeypatch, drop_everything) as sc:
        before = sc.purged_files()
        assert len(before) == 2
        assert adopt_with(monkeypatch, capsys, sc) == 2  # the reserved action alone: refused
        err = capsys.readouterr().err
        assert sc.purged_files() == before
        paths = sorted(
            line.split(" : ", 1)[1] for line in err.splitlines() if line.endswith(".cbz")
        )
        assert len(paths) == 2, err  # each file is asked for by its path
        assert adopt_with(monkeypatch, capsys, sc, "--confirm-unit", paths[0]) == 2
        capsys.readouterr()
        assert sc.purged_files() == before  # one path is not the other
        both = [x for p in paths for x in ("--confirm-unit", p)]
        assert adopt_with(monkeypatch, capsys, sc, *both) == 0
        assert sc.purged_files() == []


def test_replay_deletion_of_irreplaceable_requires_typed_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with scene(monkeypatch) as sc:
        env, settings = sc.env, sc.settings
        replaceable, irreplaceable = sc.replaceable, sc.irreplaceable
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "reconcile", "--adopt-after-restore") == 2
        out = capsys.readouterr().out
        assert irreplaceable in out and replaceable not in out  # the preview names what is at stake
        before = sc.purged_files()
        assert len(before) == 2

        # the reserved confirmation alone is not enough: nothing is deleted, nothing adopted
        assert adopt_with(monkeypatch, capsys, sc) == 2
        assert irreplaceable in capsys.readouterr().err
        assert sc.purged_files() == before
        assert db_rows(settings, "SELECT restore_generation FROM instance")[0][0] > 0  # frozen
        # a wrong identifier, another Unit's identifier and a lower-case copy are no confirmation
        for wrong in (replaceable, irreplaceable.lower(), irreplaceable[:-1], " "):
            assert adopt_with(monkeypatch, capsys, sc, "--confirm-unit", wrong) == 2, wrong
            capsys.readouterr()
            assert sc.purged_files() == before
        with env.engine.connect() as connection:  # and no audit row: nothing was confirmed
            assert not [a for a in connection.execute(sa.select(identity.audit.c.action))
                        if a.action == "reconcile.confirm_deletion"]  # fmt: skip

        assert adopt_with(monkeypatch, capsys, sc, "--confirm-unit", irreplaceable) == 0
        assert sc.purged_files() == []
        assert db_rows(settings, "SELECT restore_generation FROM instance") == [(0,)]
        audit = db_rows(settings, "SELECT action FROM audit WHERE action LIKE 'reconcile.%'")
        assert [a[0] for a in audit] == ["reconcile.confirm_deletion"]
