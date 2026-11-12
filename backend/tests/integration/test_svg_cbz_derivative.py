"""EF-BIB-051 (6): an untrusted CBZ with SVG pages becomes a declared derivative (`derived_hash`),
the original is kept in /store, and both count as independent copies (EF-BIB-027, EF-BIB-045)."""

import hashlib
import io
from contextlib import nullcontext
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.schema import platform, storage
from shelfhold.library.public import (
    KeepCopier,
    PassthroughStore,
    SafeguardFailedError,
    SvgCbzStore,
    UntrustedEntryError,
    draw_svg,
    independent_copies,
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
        env.engine,
        env.fsops,
        env.locate,
        env.data,
        tombstones=_none(),
        hold_work=lambda _work: nullcontext(),
    )
    keeper = KeepCopier(
        env.engine, env.fsops, env.locate, str(env.store), env.data,
        IntentJournal(env.engine, env.fsops), hold_work=lambda _work: nullcontext(),
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
        with pytest.raises(UntrustedEntryError):  # the plain store stays closed to SVG
            store.ingest(unit_id=unit, variant_id=variant, staged=staged, dest=str(dest))
        SvgCbzStore(store, keeper, env.tmp, env.engine, work_lock(env.engine), draw_svg).ingest(
            unit_id=unit, variant_id=variant, staged=staged, dest=str(dest)
        )
        (stored,) = env.files_under(env.store)
        assert stored.read_bytes() == original  # the bytes as received, in /store
        served = dest.read_bytes()
        assert served != original
        with safe_open_archive(io.BytesIO(served), name="x.cbz") as archive:
            assert [e.name for e in archive.entries if not e.is_dir][:2] == ["001.png", "002.png"]
        with env.engine.connect() as c:
            row = c.execute(sa.select(storage.content)).one()
            assert row.derived_hash == sha(served) and row.derived_by == "svg-rasterizer@v1"
            assert row.original_sha256 == sha(original)
            assert independent_copies(c, unit) == 2  # the original kept, the derivative served


@pytest.mark.req("EF-BIB-051")
def test_a_refused_svg_stores_nothing_and_declares_nothing() -> None:
    original = cbz_bytes(
        (("001.png", PNG), ("002.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'))
    )
    with lib_env() as env:
        store, keeper = stores(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "Dr Stone" / "Dr Stone - 1.cbz"
        staged = env.stage(original)
        with pytest.raises(UntrustedEntryError):  # drawn before anything is stored
            SvgCbzStore(store, keeper, env.tmp, env.engine, work_lock(env.engine), draw_svg).ingest(
                unit_id=unit, variant_id=variant, staged=staged, dest=str(dest)
            )
        assert not dest.exists() and env.files_under(env.store) == []
        assert staged.read_bytes() == original
        with env.engine.connect() as c:
            assert (
                c.execute(sa.select(sa.func.count()).select_from(storage.content)).scalar_one() == 0
            )


@pytest.mark.req("EF-BIB-051")
def test_a_cbz_without_svg_goes_through_the_plain_store() -> None:
    with lib_env() as env:
        store, keeper = stores(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "Dr Stone" / "Dr Stone - 1.cbz"
        SvgCbzStore(store, keeper, env.tmp, env.engine, work_lock(env.engine), draw_svg).ingest(
            unit_id=unit, variant_id=variant, staged=env.stage(cbz_bytes()), dest=str(dest)
        )
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.derived_hash)).scalar_one() is None


@pytest.mark.req("EF-BIB-051")
def test_replay_through_the_store_finishes_a_missing_derivative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sources and Suwayomi come back with the same Variant: the second call completes the
    derivative the first one could not declare, then says what the plain store says."""
    from shelfhold.library.public import VariantAlreadyStoredError

    original = cbz_bytes((("001.png", PNG), ("002.svg", SVG)))
    with lib_env() as env:
        store, keeper = stores(env)
        unit, variant = env.chain["unit"], env.chain["variant"]
        dest = env.manga / "Dr Stone" / "Dr Stone - 1.cbz"
        svg_store = SvgCbzStore(store, keeper, env.tmp, env.engine, work_lock(env.engine), draw_svg)
        real = KeepCopier.declare_derivative

        def busy(*_a: Any, **_k: Any) -> Any:
            raise SafeguardFailedError("work_busy", "held")

        staged = env.stage(original)
        monkeypatch.setattr(KeepCopier, "declare_derivative", busy)
        with pytest.raises(OSError):
            svg_store.ingest(unit_id=unit, variant_id=variant, staged=staged, dest=str(dest))
        monkeypatch.setattr(KeepCopier, "declare_derivative", real)
        with pytest.raises(VariantAlreadyStoredError):
            svg_store.ingest(unit_id=unit, variant_id=variant, staged=staged, dest=str(dest))
        with env.engine.connect() as c:
            assert c.execute(sa.select(storage.content.c.derived_hash)).scalar_one() is not None
