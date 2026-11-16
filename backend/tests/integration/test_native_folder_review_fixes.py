"""EF-EXP-010 (M4 L07, answers to the adverse review of #310): the guard also reads a moved
`path_map`, stops the write itself, and the files and the cover of Mihon's layout.

The proofs of the review are in `test_review_pr310.py` and are not touched; these tests add the
cases they leave open (the typed codes, the unknown state, a second kind of cover, the files that
Mihon adds to the tree, a volume next to a chapter of the same number).
"""

# ruff: noqa: F811  (the `world` fixture of L06a is imported, then asked for by name)
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import RootGuard
from shelfhold.export.application import cover_image
from shelfhold.export.application.cover_image import prepare_cover
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    RemoveDestination,
    removal_phrase,
)
from shelfhold.export.domain.native_folder import cover_extension
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.schema import acquisition, export
from tests.byte_source_env import unit_with_item
from tests.detach_env import ADMIN
from tests.export_env import ExportEnv, export_env
from tests.integration.test_fan_out_routes import World, world  # noqa: F401
from tests.integration.test_native_folder_pass import native, run, tree
from tests.integration.test_native_folder_root_guard import _suwayomi_source

NATIVE = "/api/v1/targets/native-folder"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
        "SHELFHOLD_DISK_PAUSE_GIB": "0",
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        "SHELFHOLD_LIBRARY_DIR": str(tmp_path / "library"),
    }


# -- M1: a moved path_map is refused with a typed code, and nothing is stored --------------------


@pytest.mark.req("EF-EXP-010")
def test_a_moved_path_map_is_refused_with_a_typed_code_and_keeps_the_old_one(world: World) -> None:
    free = world.library.parent / "native"
    free.mkdir(exist_ok=True)
    local = world.library.parent / "sw" / "local"
    local.mkdir(parents=True, exist_ok=True)
    with write_transaction(world.engine) as connection:
        connection.execute(sa.insert(acquisition.source_config).values(plugin_id="suwayomi"))
        RootGuard().record(connection, "suwayomi", str(local))
    body = {"name": "Mihon", "path_map": [{"local": str(free), "remote": "/n", "mode": "rw"}]}
    assert world.api.post(NATIVE, json=body).status_code == 201
    headers = {"X-CSRF-Token": world.api.csrf(world.api.client)}

    def move(path: Path) -> Any:
        moved = [{"local": str(path), "remote": "/n", "mode": "rw"}]
        return world.api.client.patch(
            "/api/v1/config",
            json={"values": {"path_map.Mihon": json.dumps(moved)}},
            headers=headers,
        )

    for onto in (local, local / "deep", local.parent):  # is, in, holds
        refused = move(onto)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "target.root_overlaps_local_source"
    with world.engine.connect() as connection:
        stored = connection.execute(
            sa.select(export.export_target.c.path_map).where(export.export_target.c.name == "Mihon")
        ).scalar_one()
    assert stored[0]["local"] == str(free)
    other = world.library.parent / "native-two"
    other.mkdir()
    assert move(other).status_code == 200  # a root that meets nothing moves freely


# -- M2: the write itself stops ------------------------------------------------------------------


@pytest.mark.req("EF-EXP-010")
@pytest.mark.parametrize("unreadable", [False, True])
def test_a_pass_pauses_with_a_closed_code_while_the_local_source_is_the_root_or_unknown(
    unreadable: bool,
) -> None:
    with export_env() as env:
        unit_with_item(env, env.new_series("Dr Stone"), "1")
        destination = native(env)
        fan, _source = run(env)
        fan.run(env.shelf_id, "c1")
        written = tree(env)
        assert written
        unit_with_item(env, env.new_series("Late"), "1")  # something new to write
        if unreadable:  # Suwayomi is there and what it answers cannot be read
            with write_transaction(env.engine) as connection:
                connection.execute(
                    sa.insert(acquisition.source_config).values(plugin_id="suwayomi")
                )
                RootGuard.record_unreadable(connection, "suwayomi")
            code = "target.local_source_unknown"
        else:
            _suwayomi_source(env, str(env.lib.manga / "local"))
            code = "target.root_overlaps_local_source"
        outcome = {o.destination_id: o for o in fan.run(env.shelf_id, "c2").outcomes}[destination]
        assert outcome.status == "paused" and outcome.code == code, outcome
        assert tree(env) == written  # nothing new, nothing removed
        with write_transaction(env.engine) as connection:  # the folder is told apart: it goes on
            RootGuard.record(connection, "suwayomi", str(env.lib.library.parent / "apart"))
        (env.lib.library.parent / "apart").mkdir(exist_ok=True)
        again = {o.destination_id: o for o in fan.run(env.shelf_id, "c3").outcomes}[destination]
        assert again.status == "done"
        assert len(tree(env)) > len(written)


# -- m1: any *.json of the series folder is refused by the executor ------------------------------


@pytest.mark.req("EF-EXP-010")
def test_the_destination_forbids_every_json_by_a_pattern() -> None:
    from shelfhold.export.targets.native_folder.destination import NativeFolderDestination
    from shelfhold.sdk.export_target import sidecar_forbidden

    forbidden = NativeFolderDestination.capabilities.forbidden_sidecars
    for name in ("series.json", "Anything.JSON", "x y.json", "comic.Json"):
        assert sidecar_forbidden(forbidden, name), name
    for name in ("cover.png", "Ch.0001.cbz", "json", "a.jsonx", "ComicInfo.xml"):
        assert not sidecar_forbidden(forbidden, name), name


# -- m2: what Mihon adds to the tree never refuses a removal -------------------------------------


@pytest.mark.req("EF-EXP-010")
def test_files_mihon_adds_to_a_series_folder_never_refuse_a_removal() -> None:
    """Mihon writes `ComicInfo.xml` (or `.noxml`) and `.nomedia` in a series folder and may
    rewrite the cover in place. They are not in the manifest: they are a third party's files,
    reported as such and left where they are; the chapters Shelfhold wrote go, the folder (not
    empty) stays, and nothing is refused."""
    with export_env() as env:
        unit_with_item(env, env.new_series("Dr Stone"), "1")
        destination = native(env)
        fan, _source = run(env)
        fan.run(env.shelf_id, "c1")
        folder = env.lib.manga / "local" / "Dr Stone"
        (folder / "ComicInfo.xml").write_text("<ComicInfo/>")
        (folder / ".noxml").write_text("")
        (folder / ".nomedia").write_text("")
        cover = next(folder.glob("cover.*"))
        cover.write_bytes(b"\x89PNG\r\n\x1a\n" + b"rewritten by the reader")
        remover = _remover(env)
        report = remover.apply(
            destination,
            RemovalMode.DELETE_FILES,
            principal=ADMIN,
            typed=removal_phrase(destination, RemovalMode.DELETE_FILES),
        )
        assert report.deleted == 1 and report.complete, report
        assert sorted(p.name for p in folder.iterdir()) == sorted(
            ["ComicInfo.xml", ".noxml", ".nomedia", cover.name]
        )
        assert cover.read_bytes().endswith(b"rewritten by the reader")  # never touched


def _remover(env: ExportEnv) -> RemoveDestination:
    from shelfhold.export.application.detach import RootRef

    def roots(local: str) -> RootRef | None:
        for root_id, base in (
            (env.lib.store_root, env.lib.store),
            (env.lib.library_root, env.lib.library),
        ):
            if local == str(base) or local.startswith(f"{base}/"):
                return RootRef(root_id, str(base))
        return None

    return RemoveDestination(
        env.engine,
        env.lib.fsops,
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock),
        clock=env.clock,
        roots=roots,
        locks=ResourceLocks(env.engine, clock=env.clock),
        request_scan=env.scans.append,
    )


# -- m3: the cover is named by its bytes ---------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 16
AVIF = b"\x00\x00\x00\x1cftypavif" + b"\x00" * 16


@pytest.mark.req("EF-EXP-010")
def test_the_extension_of_a_cover_is_the_one_of_its_bytes() -> None:
    assert [cover_extension(b) for b in (JPEG, PNG, WEBP, GIF, AVIF, b"")] == [
        "jpg", "png", "webp", None, None, None,
    ]  # fmt: skip
    assert prepare_cover(JPEG) == (JPEG, "jpg") and prepare_cover(PNG) == (PNG, "png")
    assert prepare_cover(WEBP) == (WEBP, "webp")


@pytest.mark.req("EF-EXP-010")
def test_a_page_in_another_format_goes_through_the_image_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    class Thumb:
        data = WEBP

    def convert(image: bytes, kind: object, **kw: Any) -> Thumb:
        seen.append({"image": image, **kw})
        return Thumb()

    monkeypatch.setattr(cover_image, "make_thumbnail", convert)
    assert prepare_cover(AVIF) == (WEBP, "webp") and prepare_cover(GIF) == (WEBP, "webp")
    assert [s["fmt"] for s in seen] == ["webp", "webp"]

    def refuse(image: bytes, kind: object, **kw: Any) -> Thumb:
        raise ImageRejected("image.undecodable", "not an image", "decode")

    monkeypatch.setattr(cover_image, "make_thumbnail", refuse)
    assert prepare_cover(AVIF) is None  # no cover is better than a wrong one


@pytest.mark.req("EF-EXP-010")
@pytest.mark.parametrize(
    ("first_page", "name"), [(JPEG, "cover.jpg"), (WEBP, "cover.webp"), (PNG, "cover.png")]
)
def test_a_pass_names_the_cover_by_the_bytes_of_the_first_page(
    monkeypatch: pytest.MonkeyPatch, first_page: bytes, name: str
) -> None:
    monkeypatch.setattr("shelfhold.export.application.fan_out.first_page", lambda _data: first_page)
    with export_env() as env:
        unit_with_item(env, env.new_series("Dr Stone"), "1")
        native(env)
        fan, _source = run(env)
        fan.run(env.shelf_id, "c1")
        assert f"Dr Stone/{name}" in tree(env)
        assert (env.lib.manga / "local" / "Dr Stone" / name).read_bytes() == first_page
        fan.run(env.shelf_id, "c2")  # a cover that is there is not made again, whatever its name
        assert sorted(p for p in tree(env) if "cover." in p) == [f"Dr Stone/{name}"]


# -- m5: a volume is not the chapter of the same number ------------------------------------------


@pytest.mark.req("EF-EXP-010")
def test_volume_3_is_not_chapter_3_for_mihon() -> None:
    """Mihon takes the number of a chapter from the `Number` of the ComicInfo, and cuts `Vol.N`
    out of the name. The ComicInfo of a volume therefore never says `3` for volume 3: it says the
    number of its first chapter, so that chapter 3 (a file of its own) stays the only one."""
    with export_env() as env:
        series = env.new_series("Dr Stone")
        chapter_three = unit_with_item(env, series, "3")
        first, second = env.add_chapter(series, "21"), env.add_chapter(series, "22")
        volume = new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(export.export_item).values(
                    id=volume, edition_id=series.edition_id, kind="volume",
                    frozen_attrs={"number_norm": "3", "volume_label": "Vol.3"}, closed=True,
                )
            )  # fmt: skip
            for position, unit in enumerate((first, second)):
                connection.execute(
                    sa.insert(export.unit_coverage).values(
                        carrier_type="export_item", carrier_id=volume, unit_id=unit,
                        position=position, extent="full", origin="provider",
                    )
                )  # fmt: skip
        native(env)
        fan, source = run(env)
        fan.run(env.shelf_id, "c1")
        files = [p for p in tree(env) if p.endswith(".cbz")]
        assert files == ["Dr Stone/Ch.0003.cbz", "Dr Stone/Vol.0003.cbz"], files

        def number_of(rel: str) -> str:
            data = (env.lib.manga / "local" / rel).read_bytes()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.read("ComicInfo.xml").decode()
            found = [t for t in info.split("<Number>")[1:]]
            return found[0].split("</Number>")[0] if found else ""

        assert number_of("Dr Stone/Ch.0003.cbz") == "3"
        assert number_of("Dr Stone/Vol.0003.cbz") != "3"
        assert chapter_three.item_id and isinstance(source, object)
