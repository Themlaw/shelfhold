"""`shelfhold detach --full` and `shelfhold target remove` as the admin runs them (M3 L30,
EF-EXP-031), over an instance built by the production services: a Kavita library adopted with
`shelfhold adopt` against the simulated Kavita, the real roots, the real journal. Nothing here
stubs the channel or the services."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import export
from tests.l54_env import SERIES, Bench54, bench54, comic


def _adopted(b: Bench54, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    b.kavita_series(SERIES, [f"{SERIES} - Ch.1.cbz", f"{SERIES} - Ch.2.cbz"], info=comic(SERIES))
    assert b.adopt(capsys) == 0
    with b.engine.connect() as conn:
        target = str(conn.execute(sa.select(export.export_target.c.id)).scalar_one())
        destination = str(conn.execute(sa.select(export.shelf_destination.c.id)).scalar_one())
    return target, destination


def _hidden(b: Bench54) -> list[str]:
    root = b.env.inst.library
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.name.startswith(".shelfhold")
    )


def _files(b: Bench54) -> dict[str, bytes]:
    root = b.env.inst.library / "Kavita"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*.cbz"))}


def _library_patterns(b: Bench54) -> list[str]:
    return list(b.env.inst.kavita.libraries[0]["excludePatterns"])


@pytest.mark.req("EF-EXP-031")
def test_detach_full_command_shows_then_does_nothing_without_the_phrase(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        target, _destination = _adopted(b, capsys)
        patterns, hidden, files = _library_patterns(b), _hidden(b), _files(b)
        assert hidden and patterns
        capsys.readouterr()
        assert b.cli("detach", "--full", "--target", target) == 2  # shows, asks
        shown = capsys.readouterr()
        assert f"detach {target}" in shown.err  # the phrase to type
        assert b.cli("detach", "--full", "--target", target, "--confirm", "yes") != 0
        assert b.cli("detach", "--full") == 2  # no target named
        assert (_library_patterns(b), _hidden(b), _files(b)) == (patterns, hidden, files)
        with b.engine.connect() as conn:  # still attached
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(export.export_target)).scalar()
                == 1
            )


@pytest.mark.req("EF-EXP-031")
def test_detach_full_command_restores_settings_erases_hidden_files_and_keeps_the_tree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        target, _destination = _adopted(b, capsys)
        with b.engine.connect() as conn:
            before = conn.execute(
                sa.select(export.shelf_destination.c.pre_adoption_settings)
            ).scalar_one()
        files = _files(b)
        capsys.readouterr()
        # no password given: the key cannot be revoked, and the command says so and stops short
        assert b.cli("detach", "--full", "--target", target, "--confirm", f"detach {target}") == 3
        shown = capsys.readouterr()
        assert "clé dédiée" in shown.err  # NOT done: the dedicated key was not revoked
        assert _hidden(b) == []  # the reserved folder and the sentinel are gone
        assert _library_patterns(b) == before["exclude_patterns"]  # as before the adoption
        assert _files(b) == files  # the admin's tree is whole, byte for byte
        with b.engine.connect() as conn:  # the manifest waits for the key
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(export.export_target)).scalar()
                == 1
            )
        assert "Laissé en place" in shown.out  # the report of what stays


@pytest.mark.req("EF-EXP-031", "EF-USR-024")
def test_remove_destination_command_keep_files_asks_then_forgets_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        _target, destination = _adopted(b, capsys)
        files, hidden = _files(b), _hidden(b)
        capsys.readouterr()
        assert b.cli("target", "remove", destination, "--keep-files") == 2
        assert f"remove --keep-files {destination}" in capsys.readouterr().err
        assert b.cli("target", "remove", destination, "--keep-files", "--confirm", "no") != 0
        with pytest.raises(SystemExit):  # one of the two ways is required
            b.cli("target", "remove", destination)
        with b.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(export.shelf_destination)
                ).scalar()
                == 1
            )
        assert (
            b.cli(
                "target",
                "remove",
                destination,
                "--keep-files",
                "--confirm",
                f"remove --keep-files {destination}",
            )
            == 0
        )
        assert (_files(b), _hidden(b)) == (files, hidden)  # nothing on disk was touched
        with b.engine.connect() as conn:
            assert (
                conn.execute(
                    sa.select(sa.func.count()).select_from(export.shelf_destination)
                ).scalar()
                == 0
            )
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(export.export_record)).scalar()
                == 0
            )
