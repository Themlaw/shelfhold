"""M5 L20b (EF-BIB-015, volet KEPUB, ADR-0048): the KEPUB rendition of an EPUB, in the cache of L20a.

The EPUB is the neutralized derivative the caller read for a content it has already judged visible
(rule X4 of the renditions). The conversion is `kepubify`, pure Python, run at most a few at a time;
the result is kept under `/data/cache/renditions/<aa>/<hash>.kepub`, named by its own hash, keyed by
the hash of the bytes that were converted, and gone with its source. Synthetic books only."""

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import storage
from shelfhold.library.application.kepub import KepubError, kepubify
from shelfhold.library.application.renditions import (
    RenditionBusyError,
    RenditionService,
    SourceTombstones,
)
from shelfhold.library.domain.failures import TombstonedContentError
from shelfhold.library.domain.rendition import RenditionParams, target_kind
from tests.export_env import ExportEnv, export_env
from tests.fakes_clock import ManualClock
from tests.null_tombstones import NONE_PURGED
from tests.purge_env import ADMIN, TITLE, make_purge_env, spread
from tests.unit.test_kepub import book, epub

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)
KEPUB = "application/kepub+zip"


class Counting:
    """The real conversion, counting how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, data: bytes) -> bytes:
        self.calls += 1
        return kepubify(data)


def _service(
    env: ExportEnv,
    convert: Any = None,
    clock: Clock | None = None,
    tombstones: SourceTombstones | None = None,
    **limits: int,
) -> RenditionService:
    return RenditionService(
        env.engine,
        env.lib.data,
        clock or ManualClock(NOW),
        tombstones=tombstones or NONE_PURGED,
        kepub=convert,
        **limits,
    )


def _content(env: ExportEnv, unit: str) -> str:
    with env.engine.connect() as connection:
        return connection.execute(
            sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit)
        ).scalar_one()


def _rows(env: ExportEnv) -> list[sa.Row[tuple[str, ...]]]:
    with env.engine.connect() as connection:
        return list(connection.execute(sa.text("SELECT * FROM rendition ORDER BY created_at")))


def _files(env: ExportEnv) -> list[Path]:
    return sorted(p for p in (env.lib.data / "cache" / "renditions").rglob("*") if p.is_file())


@pytest.mark.req("EF-BIB-015")
def test_renditions_kepub_and_image_conversion_kepub() -> None:
    """The KEPUB volet: an EPUB becomes a KEPUB once, kept by the hash of what was made."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        service = _service(env, counting)
        source = epub(book())

        made = service.render_kepub(_content(env, unit), source)
        again = service.render_kepub(_content(env, unit), source)

        assert made == again and counting.calls == 1  # the second ask is the cache
        assert made.kind == "kepub" and made.media_type == KEPUB
        data = service.read(made)
        assert data is not None and len(data) == made.size
        assert data == kepubify(source)  # what the pure core makes, byte for byte
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            assert b"koboSpan" in archive.read("OEBPS/chapter1.xhtml")
        (row,) = _rows(env)
        assert row.kind == "kepub" and row.content_hash == made.content_hash
        (file,) = _files(env)
        assert file.name == f"{made.content_hash.removeprefix('sha256:')}.kepub"
        assert service.kepub_bytes(_content(env, unit), source) == data


@pytest.mark.req("EF-BIB-015")
def test_a_kepub_is_never_planned_for_an_image() -> None:
    """The closed list of image kinds is not widened: `kepub` is no target of a page."""
    assert target_kind("kepub") is None and target_kind("application/kepub+zip") is None
    with pytest.raises(ValueError):
        RenditionParams(kind="gif")


@pytest.mark.req("EF-BIB-015")
def test_the_kepub_cache_is_keyed_by_the_bytes_that_were_converted() -> None:
    """Another version of the book handed for the same content never gets the first one's answer;
    the cache is not a way to read what another profile's copy held."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        service = _service(env, counting)
        one = service.render_kepub(_content(env, unit), epub(book()))
        two = service.render_kepub(_content(env, unit), epub(book(**{"OEBPS/style.css": b"p{}"})))
        assert one.content_hash != two.content_hash and counting.calls == 2
        assert len(_rows(env)) == 2 and len(_files(env)) == 2


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_a_kepub_is_purged_with_its_source_and_only_it() -> None:
    """`purge --now` of a Unit takes its KEPUB, row and file (ADR-0048)."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        other = env.add_chapter(series, "2")
        service = _service(env)
        service.render_kepub(_content(env, unit), epub(book()))
        kept = service.render_kepub(
            _content(env, other), epub(book(**{"OEBPS/style.css": b"other"}))
        )
        assert len(_rows(env)) == 2 and len(_files(env)) == 2

        report = purge.purge.purge_unit(unit, now=purge.clock.now(), principal=ADMIN, typed=unit)

        assert report.complete
        assert [r.content_hash for r in _rows(env)] == [kept.content_hash]
        assert [f.name.split(".")[0] for f in _files(env)] == [
            kept.content_hash.removeprefix("sha256:")
        ]


class _Tombstoned:
    def check_content(self, hashes: object = ()) -> bool:
        return True

    def has_values(self, candidate: object) -> bool:
        return True


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_nothing_is_made_nor_handed_back_for_a_source_whose_purge_has_begun() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        content = _content(env, unit)
        source = epub(book())
        made = _service(env).render_kepub(content, source)
        counting = Counting()
        gone = _service(env, counting, tombstones=_Tombstoned())
        with pytest.raises(TombstonedContentError):
            gone.render_kepub(content, source)
        with pytest.raises(TombstonedContentError):
            gone.kepub_bytes(content, source)
        assert counting.calls == 0
        assert gone.cached(content, RenditionParams(kind="kepub"), source) is None
        assert made.content_hash  # the one made before is a row the purge itself erases


@pytest.mark.req("EF-BIB-015")
def test_a_book_that_cannot_be_converted_leaves_neither_row_nor_file() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        service = _service(env)
        for bad in (b"not an archive", epub({"META-INF/container.xml": b"<x/>"})):
            with pytest.raises(KepubError):
                service.render_kepub(_content(env, unit), bad)
        assert _rows(env) == [] and _files(env) == []


@pytest.mark.req("EF-BIB-015")
def test_a_book_over_the_input_bound_is_refused_before_it_is_read() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        service = _service(env, counting, max_kepub_bytes=1000)
        with pytest.raises(KepubError) as refused:
            service.render_kepub(_content(env, unit), b"PK" + b"\x00" * 2000)
        assert refused.value.reason == "too_large" and counting.calls == 0
        assert _rows(env) == []


@pytest.mark.req("EF-BIB-015")
def test_the_orphan_sweep_knows_a_kepub_file_by_its_name() -> None:
    """A file a row names stays; a stray one goes after the grace, as for an image."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        clock = ManualClock(NOW)
        service = _service(env, clock=clock)
        made = service.render_kepub(_content(env, unit), epub(book()))
        stray = env.lib.data / "cache" / "renditions" / "ab" / ("c" * 64 + ".kepub")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"left by a crash")
        import os

        old = NOW.timestamp() - 3600
        os.utime(stray, (old, old))
        assert service.collect_orphans() == 1
        assert not stray.exists() and service.read(made) is not None


# -- the bound on threads shared by every user of the cache (L18 observation, decided here) -------


@pytest.mark.req("EF-BIB-015")
def test_the_slots_are_bounded_in_all_and_per_profile_and_given_back() -> None:
    with export_env() as env:
        service = _service(env, max_held=3, max_per_owner=2)
        with service.slot("mum"):
            with service.slot("mum"):
                with pytest.raises(RenditionBusyError) as busy:
                    with service.slot("mum"):  # a third for one profile: refused at once
                        raise AssertionError("unreachable")
                assert busy.value.retry_after_s >= 1
                with service.slot("kid"):  # another profile is served: the bound is per profile
                    with pytest.raises(RenditionBusyError), service.slot("dad"):  # all: 3 held
                        raise AssertionError("unreachable")
        with service.slot("mum"), service.slot("mum"), service.slot("kid"):
            pass  # everything was given back


@pytest.mark.req("EF-BIB-015")
def test_a_slot_is_given_back_when_the_conversion_fails() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        service = _service(env, max_held=1, max_per_owner=1)
        for _ in range(3):
            with pytest.raises(KepubError), service.slot("mum"):
                service.render_kepub(_content(env, unit), b"not an archive")
        with service.slot("mum"):
            pass


@pytest.mark.req("EF-BIB-015")
def test_conversions_run_two_at_a_time_and_the_waiter_is_told_to_come_back() -> None:
    import threading

    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        started, release = threading.Event(), threading.Event()

        def slow(data: bytes) -> bytes:
            started.set()
            release.wait(5)
            return kepubify(data)

        service = _service(env, slow, max_converting=1, wait_s=0.05)
        content = _content(env, unit)
        first = threading.Thread(
            target=lambda: service.render_kepub(content, epub(book())), daemon=True
        )
        first.start()
        assert started.wait(5)
        with pytest.raises(RenditionBusyError):
            service.render_kepub(content, epub(book(**{"OEBPS/style.css": b"x"})))
        release.set()
        first.join(5)
        assert len(_rows(env)) == 1
