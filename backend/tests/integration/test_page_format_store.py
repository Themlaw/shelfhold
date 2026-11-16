"""M4 L53 (EF-EXP-046 amended by ADR-0054): the pages of an archive that comes in are normalized.

The canonical copy is the received file: kept byte for byte in /store when /store is configured,
and /library holds the normalized derivative (`derived_by = page-normalizer@v1`), the two counting
as independent copies. Without /store nothing is rewritten in place (a lossy rewrite with no
original is not undoable): the archive stays as received and is normalized on the way out.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from contextlib import nullcontext
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.packaging.cbz import PageTarget
from shelfhold.infrastructure.schema import platform, storage
from shelfhold.library.public import (
    KeepCopier,
    PageFormatCbzStore,
    PassthroughStore,
    SvgCbzStore,
    VariantAlreadyStoredError,
    draw_svg,
    independent_copies,
)
from tests import page_formats_env as pf
from tests.library_env import LibEnv, lib_env
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_store(
    env: LibEnv, *, keeps_originals: bool = True, issues: list[Any] | None = None
) -> PageFormatCbzStore:
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(platform.instance).values(instance_id=env.instance_id, jurisdiction="FR")
        )
    plain = PassthroughStore(
        env.engine, env.fsops, env.locate, env.data,
        tombstones=NONE_PURGED, hold_work=lambda _work: nullcontext(),
    )  # fmt: skip
    keeper = KeepCopier(
        env.engine, env.fsops, env.locate, str(env.store), env.data,
        IntentJournal(env.engine, env.fsops), hold_work=lambda _work: nullcontext(),
    )  # fmt: skip
    svg = SvgCbzStore(plain, keeper, env.tmp, env.engine, work_lock(env.engine), draw_svg)
    return PageFormatCbzStore(
        svg, plain, keeper, env.tmp, env.engine, work_lock(env.engine),
        target=PageTarget(), keeps_originals=keeps_originals,
        on_issues=None if issues is None else (lambda unit, found: issues.append((unit, found))),
    )  # fmt: skip


def entry_names(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [i.filename for i in archive.infolist()]


@pytest.mark.req("EF-EXP-046", "EF-BIB-027")
def test_avif_archive_is_kept_in_store_and_library_holds_the_normalized_derivative() -> None:
    original = pf.mixed_cbz()
    with lib_env() as env:
        store = make_store(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "S" / "S - 1.cbz"
        store.ingest(unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest))
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == original  # the canonical copy: byte for byte
        served = dest.read_bytes()
        assert entry_names(served) == ["001.jpg", "002.png", "003.jpg", "004.webp", "ComicInfo.xml"]
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert row.original_sha256 == sha(original)
            assert row.derived_hash == sha(served) and row.derived_by == "page-normalizer@v1"
            assert independent_copies(c, unit) == 2


@pytest.mark.req("EF-EXP-046")
def test_without_a_store_the_library_keeps_what_was_received() -> None:
    original = pf.mixed_cbz()
    with lib_env() as env:
        store = make_store(env, keeps_originals=False)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "S" / "S - 1.cbz"
        store.ingest(unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest))
        assert dest.read_bytes() == original and env.files_under(env.store) == []
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.derived_hash)).scalar_one() is None


@pytest.mark.req("EF-EXP-046")
def test_an_archive_already_in_allowed_formats_is_not_rewritten() -> None:
    original = pf.cbz([("001.jpg", pf.jpeg()), ("002.png", pf.png_alpha(10))])
    with lib_env() as env:
        store = make_store(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "S" / "S - 1.cbz"
        store.ingest(unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest))
        assert dest.read_bytes() == original
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.derived_hash)).scalar_one() is None


@pytest.mark.req("EF-EXP-046")
def test_a_replay_does_not_rewrite_the_derivative() -> None:
    original = pf.mixed_cbz()
    with lib_env() as env:
        store = make_store(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "S" / "S - 1.cbz"
        store.ingest(unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest))
        before = dest.read_bytes()
        with pytest.raises(VariantAlreadyStoredError):
            store.ingest(
                unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest)
            )
        assert dest.read_bytes() == before  # same bytes, same hash
        assert not store.derivative_missing(unit, env.stage(original))


@pytest.mark.req("EF-EXP-046", "ENF-SEC-003")
def test_an_archive_with_an_unreadable_page_is_filed_as_received_and_reported() -> None:
    original = pf.cbz([("001.avif", pf.avif()), ("002.avif", pf.truncated_avif())])
    seen: list[Any] = []
    with lib_env() as env:
        store = make_store(env, issues=seen)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "S" / "S - 1.cbz"
        store.ingest(unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest))
        assert dest.read_bytes() == original
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.derived_hash)).scalar_one() is None
    assert [(i.entry, i.code) for _, found in seen for i in found] == [
        ("002.avif", "page.undecodable")
    ]


@pytest.mark.req("EF-EXP-046")
def test_a_page_in_an_unusual_format_is_accepted_at_the_door_and_converted() -> None:
    original = pf.cbz([("001.bmp", pf.bmp()), ("002.tif", pf.tiff()), ("003.jpg", pf.jpeg())])
    with lib_env() as env:
        store = make_store(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "S" / "S - 1.cbz"
        store.ingest(unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest))
        assert entry_names(dest.read_bytes()) == ["001.jpg", "002.jpg", "003.jpg"]
        (kept,) = env.files_under(env.store)
        assert kept.read_bytes() == original
