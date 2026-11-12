"""M2 wiring (L26, EF-BIB-044, EF-LEC-014): `shelfhold export item show <id>`, the display of the
kept file of an export item (a volume under `/data/ranged/`). Only the id is read from the
caller; a path, a link or a folder that is not the folder of the ranges is refused and not one
byte of it is read."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.auth_env import run_cli
from tests.l50_env import Seeded, seeded
from tests.ranged_env import volume_epub

pytestmark = pytest.mark.req("EF-BIB-044", "EF-LEC-014")


def item_of(env: Seeded, label: str | None = "Vol.1", closed: bool = True) -> str:
    series = env.new_series("Un roman")
    unit_ids = [env.add_chapter(series, str(n)) for n in (1, 2)]
    item = new_ulid()
    with env.inst.engine.begin() as connection:
        connection.execute(sa.insert(export.export_item).values(
            id=item, edition_id=series.edition_id, kind="volume", closed=closed,
            frozen_attrs={"volume_label": label} if label else {}))  # fmt: skip
        for position, unit in enumerate(unit_ids):
            connection.execute(sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=position,
                extent="full", origin="provider"))  # fmt: skip
    return item


def test_show_names_the_item_its_volume_its_chapters_and_the_kept_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        item = item_of(env)
        kept = settings.data_dir / "ranged"
        kept.mkdir(exist_ok=True)
        (kept / f"{item}.epub").write_bytes(volume_epub(2))
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "export", "item", "show", item) == 0
        out = capsys.readouterr().out
        assert item in out and "Vol.1" in out and str(kept / f"{item}.epub") in out
        assert "2" in out  # the chapters it carries


def test_show_says_when_the_kept_file_is_missing_and_refuses_an_unknown_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        item = item_of(env, label=None, closed=False)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "export", "item", "show", item) == 0
        assert "ranged.missing" in capsys.readouterr().out  # told, not hidden
        assert run_cli(monkeypatch, settings, "export", "item", "show", new_ulid()) != 0
        for hostile in ("../../etc/passwd", "x", "", item + "/../..", item.lower(), "%00"):
            assert run_cli(monkeypatch, settings, "export", "item", "show", hostile) != 0


def test_show_never_follows_a_link_nor_reads_what_is_not_the_kept_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        item = item_of(env)
        outside = tmp_path / "secret.epub"
        outside.write_bytes(b"outside")
        kept = settings.data_dir / "ranged"
        kept.mkdir(exist_ok=True)
        (kept / f"{item}.epub").symlink_to(outside)  # a link at the end
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "export", "item", "show", item) == 0
        shown = capsys.readouterr()
        assert str(outside) not in shown.out + shown.err
        assert "ranged.outside_root" in shown.out or "ranged.missing" in shown.out
        (kept / f"{item}.epub").unlink()
        (kept / f"{item}.epub").mkdir()  # a folder in place of the file
        assert run_cli(monkeypatch, settings, "export", "item", "show", item) == 0
        assert str(kept / f"{item}.epub") not in capsys.readouterr().out.split("ranged.")[0]


def test_export_without_a_subcommand_still_needs_its_destination(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    assert run_cli(monkeypatch, settings, "export", "--dry-run") != 0
