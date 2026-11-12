"""EF-BIB-051 (6): an untrusted CBZ with SVG pages becomes a declared derivative (`derived_hash`),
the original is kept in /store, and both count as independent copies (EF-BIB-027, EF-BIB-045)."""

import hashlib
import io

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.imaging import rasterize_svg
from shelfhold.infrastructure.schema import platform, storage
from shelfhold.library.public import (
    KeepCopier,
    PassthroughStore,
    SafeguardFailedError,
    UntrustedEntryError,
    independent_copies,
    rasterize_svg_pages,
)
from tests.library_env import PNG, LibEnv, cbz_bytes, lib_env
from tests.locks_env import work_lock

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="6" height="6">'
    b'<rect width="6" height="6" fill="#123456"/></svg>'
)


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def stores(env: LibEnv) -> tuple[PassthroughStore, KeepCopier]:
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(platform.instance).values(instance_id=env.instance_id, jurisdiction="FR")
        )
    store = PassthroughStore(
        env.engine, env.fsops, env.locate, env.data, tombstones=_none(), hold_work=work_lock(env.engine)
    )
    keeper = KeepCopier(
        env.engine, env.fsops, env.locate, str(env.store), env.data,
        IntentJournal(env.engine, env.fsops), hold_work=work_lock(env.engine),
    )  # fmt: skip
    return store, keeper


def _none() -> object:
    from tests.null_tombstones import NONE_PURGED

    return NONE_PURGED


@pytest.mark.req("EF-BIB-051", "EF-BIB-027")
def test_rasterized_cbz_is_declared_derivative_counted_by_independent_copies() -> None:
    original = cbz_bytes((("001.png", PNG), ("002.svg", SVG)))
    with lib_env() as env:
        store, keeper = stores(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "Dr Stone" / "Dr Stone - 1.cbz"
        staged = env.stage(original)
        with pytest.raises(UntrustedEntryError):  # closed unless the caller opts in
            store.ingest(unit_id=unit, variant_id=variant, staged=staged, dest=str(dest))
        store.ingest(
            unit_id=unit, variant_id=variant, staged=staged, dest=str(dest), allow_svg_pages=True
        )
        derived = rasterize_svg_pages(
            keeper, unit, dest, env.tmp, lambda data: rasterize_svg(data).png
        )
        assert derived.original_sha256 == sha(original)
        (stored,) = env.files_under(env.store)
        assert stored.read_bytes() == original  # the bytes as received, in /store
        served = dest.read_bytes()
        assert sha(served) == derived.derived_sha256 and served != original
        with safe_open_archive(io.BytesIO(served), name="x.cbz") as archive:
            assert [e.name for e in archive.entries if not e.is_dir][:2] == ["001.png", "002.png"]
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert row.derived_hash == sha(served) and row.derived_by == "svg-rasterizer@v1"
            assert row.original_sha256 == sha(original)
            assert independent_copies(c, unit) == 2  # the original kept, the derivative served


@pytest.mark.req("EF-BIB-051")
def test_a_refused_svg_leaves_library_untouched_and_declares_nothing() -> None:
    original = cbz_bytes(
        (("001.png", PNG), ("002.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'))
    )
    with lib_env() as env:
        store, keeper = stores(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "Dr Stone" / "Dr Stone - 1.cbz"
        store.ingest(
            unit_id=unit, variant_id=variant, staged=env.stage(original), dest=str(dest),
            allow_svg_pages=True,
        )
        with pytest.raises(SafeguardFailedError):
            rasterize_svg_pages(keeper, unit, dest, env.tmp, lambda data: rasterize_svg(data).png)
        assert dest.read_bytes() == original
        assert env.files_under(env.store) == []
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.derived_hash)).scalar_one() is None
