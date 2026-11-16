"""Relecture adverse de la PR #311 (M4 L53, normalisation des pages) : PoC rouges.

Chaque test décrit le comportement attendu. Il échoue aujourd'hui pour la raison donnée dans
`docs/decisions/M4.md`, section « #311 : contradicteur ». Le `xfail(strict=True)` signalera le jour
où il passe.
"""

from __future__ import annotations

import io
import zipfile

import pytest
import sqlalchemy as sa
from PIL import Image

from shelfhold.acquisition.public import RootGuard
from shelfhold.export.application.export_items import create_export_item
from shelfhold.export.application.fan_out import FanOut, HostContentStore
from shelfhold.export.application.rendering import CbzRenderer
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.export.targets.native_folder.source import NativeFolderSource
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import KEEP_PAGE_KINDS, sniff_page_kind
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.imaging import reencode_image
from shelfhold.infrastructure.packaging.cbz import normalize_cbz
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.library.domain.content_hash import content_hash
from shelfhold.library.public import PassthroughStore, independent_copies
from tests import page_formats_env as pf
from tests.export_env import ExportEnv, export_env
from tests.fan_out_env import add_target, bind
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED
from tests.plan_executor_env import Probe

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")


def _avif_unit(env: ExportEnv, number: str = "1") -> tuple[str, str]:
    """A chapter with AVIF pages, filed as received (a library from before L53, or no /store)."""
    series = env.new_series("Dr Stone")
    with write_transaction(env.engine) as connection:
        unit = add_unit(connection, series.edition_id, number)
        variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
    PassthroughStore(
        env.lib.engine, env.lib.fsops, env.lib.locate, env.lib.data,
        tombstones=NONE_PURGED, hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=unit, variant_id=variant,
        staged=env.lib.stage(pf.cbz([("001.avif", pf.avif()), ("002.avif", pf.avif((1, 2, 3)))])),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{unit}.cbz"),
    )  # fmt: skip
    with write_transaction(env.engine) as connection:
        item = create_export_item(
            connection, edition_id=series.edition_id, unit_id=unit, kind="unit", frozen_attrs={}
        )
    return unit, item


def _normalize_library(env: ExportEnv) -> None:
    """`library normalize-pages`, as the composition root wires it, with /store configured."""
    summary = pf.build_normalizer(
        env.engine, env.lib.fsops, env.lib.locate, store=str(env.lib.store), data=env.lib.data,
        workdir=env.lib.tmp,
    ).run()  # fmt: skip
    assert summary.normalized >= 1, summary


def _kinds(data: bytes) -> set[str | None]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {
            sniff_page_kind(archive.read(n)[:32])
            for n in archive.namelist()
            if not n.lower().endswith(".xml")
        }


def _native_pass(env: ExportEnv) -> str:
    destination = bind(
        env, "local", target_id=add_target(env, kind="native_folder", partitioning="none")
    )
    source = NativeFolderSource(env.engine, root_check=RootGuard().check_root)
    host = HostContentStore(env.engine, capabilities=source.capabilities, renders=source.renders)
    source.attach(host)
    env.pipeline.use_plan_executor(contents=host, probe=Probe(None))
    report = FanOut(env.engine, env.pipeline, source, clock=env.clock).run(env.shelf_id, "c1")
    outcome = {o.destination_id: o for o in report.outcomes}[destination]
    assert outcome.status == "done", outcome
    return destination


# -- B1 : un chapitre normalisé dans /library ne s'exporte plus ------------------------------------


@pytest.mark.req("EF-EXP-046", "EF-BIB-027")
def test_a_chapter_normalized_in_the_library_is_still_exported() -> None:
    with export_env() as env:
        _avif_unit(env)
        _normalize_library(env)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (written,) = env.files()
        assert _kinds(written.read_bytes()) <= KEEP_PAGE_KINDS


# -- B2 : le dossier natif (L07), servi par l'hôte, reçoit de l'AVIF ----------------------------


@pytest.mark.req("EF-EXP-046", "EF-EXP-010")
def test_the_native_folder_gets_only_allowlisted_pages_without_store() -> None:
    with export_env() as env:
        _avif_unit(env)
        _native_pass(env)
        chapters = list((env.lib.manga / "local").rglob("*.cbz"))
        assert chapters
        for chapter in chapters:
            assert _kinds(chapter.read_bytes()) <= KEEP_PAGE_KINDS


@pytest.mark.req("EF-EXP-046", "EF-EXP-010", "EF-BIB-045")
def test_the_native_folder_gets_only_allowlisted_pages_after_normalize_pages() -> None:
    with export_env() as env:
        _avif_unit(env)
        _normalize_library(env)
        _native_pass(env)
        chapters = list((env.lib.manga / "local").rglob("*.cbz"))
        assert chapters
        for chapter in chapters:
            assert _kinds(chapter.read_bytes()) <= KEEP_PAGE_KINDS


# -- B3 : `normalize-pages` ne peut plus rattraper un fichier déjà servi -------------------------


@pytest.mark.req("EF-EXP-046", "EF-EXP-016")
def test_normalize_pages_rerenders_a_served_file_once_the_library_is_normalized() -> None:
    from shelfhold.export.application.page_policy import PagePolicy
    from shelfhold.export.application.upgrade import (
        SamePathUpgrade,
        UpgradeOutcome,
        UpgradeRequest,
    )

    with export_env() as env:
        unit, _ = _avif_unit(env)
        env.pipeline._renderer = CbzRenderer(  # servi avant L53 : pages AVIF
            env.lib.fsops, staging_dir=env.lib.data / "tmp",
            pages=PagePolicy(keep=KEEP_PAGE_KINDS | {"avif"}),
        )  # fmt: skip
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (served,) = env.files()
        _normalize_library(env)  # ce que fait `normalize-pages` avant de rattraper les fichiers
        with env.engine.connect() as connection:
            content = connection.execute(
                sa.text("SELECT id FROM content WHERE unit_id = :u"), {"u": unit}
            ).scalar_one()
            item = connection.execute(
                sa.text("SELECT export_item_id FROM export_record WHERE shelf_destination_id = :d"),
                {"d": env.destination_id},
            ).scalar_one()
        upgrade = SamePathUpgrade(
            env.engine, env.lib.fsops, locate=env.lib.locate, data_dir=env.lib.data,
            clock=env.clock, renderer=CbzRenderer(env.lib.fsops, staging_dir=env.lib.data / "tmp"),
        )  # fmt: skip
        asked = UpgradeRequest(
            env.destination_id, item, str(content), "better_version", rerender=True
        )
        assert upgrade.upgrade(asked).outcome is UpgradeOutcome.UPGRADED
        assert _kinds(served.read_bytes()) <= KEEP_PAGE_KINDS


# -- M1 : le registre dit que le dérivé avec perte porte la charge utile de l'original ------------


@pytest.mark.req("EF-BIB-027", "EF-BIB-045")
def test_the_registry_does_not_say_a_lossy_derivative_holds_the_original_payload() -> None:
    with export_env() as env:
        unit, _ = _avif_unit(env)
        _normalize_library(env)
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    replica_schema.replica.c.content_hash, replica_schema.replica.c.rel_path
                ).where(
                    replica_schema.replica.c.unit_id == unit,
                    replica_schema.replica.c.location == "library",
                )
            ).one()
            faithful = 1  # /store seul garde les octets reçus
            counted = independent_copies(connection, unit)
        library_file = env.lib.library / row.rel_path
        with safe_open_archive(library_file, name="library.cbz") as archive:
            actual = content_hash(archive)
        assert row.content_hash == actual  # le registre dit vrai sur la copie de /library
        assert counted == faithful  # ou un compte séparé des copies fidèles


# -- M2 : TIFF 16 bits et TIFF multipage ----------------------------------------------------------


def _tiff16(value: int) -> bytes:
    image = Image.new("I;16", (64, 48), value)
    out = io.BytesIO()
    image.save(out, "TIFF")
    return out.getvalue()


@pytest.mark.req("EF-EXP-046")
def test_a_16_bit_grey_tiff_page_is_not_turned_white() -> None:
    result = reencode_image(_tiff16(0x4000))  # un gris sombre (25 %)
    pixel = Image.open(io.BytesIO(result.data)).convert("L").getpixel((10, 10))
    assert isinstance(pixel, int) and pixel < 128, pixel


@pytest.mark.req("EF-EXP-046")
def test_a_multi_page_tiff_is_not_silently_cut_to_its_first_page() -> None:
    frames = [Image.new("RGB", (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, "TIFF", save_all=True, append_images=frames[1:])
    archive = pf.cbz([("001.tif", out.getvalue())])
    with safe_open_archive(io.BytesIO(archive), name="in.cbz") as source:
        sink = io.BytesIO()
        report = normalize_cbz(source, sink)
    # soit refusé (`kept`, code fermé), soit trois pages : jamais deux pages perdues sans rien dire
    if report.status == "kept":
        return
    with zipfile.ZipFile(io.BytesIO(sink.getvalue())) as written:
        assert len([n for n in written.namelist() if not n.endswith(".xml")]) == 3
