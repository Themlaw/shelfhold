"""M5 L20a (EF-BIB-015, ADR-0054, ADR-0048): image renditions for a client that lacks the format.

The bytes are untrusted: they are bounded by their header before a worker starts, decoded only in
the network-less worker, kept under a name that is the hash of what was made, and gone with their
source. Synthetic images only."""

import io
import os
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from PIL import Image

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.imaging import ImageCaps, ImageRejected, Reencoded, reencode_image
from shelfhold.infrastructure.schema import storage
from shelfhold.library.application.renditions import (
    RenditionService,
    SourceTombstones,
    UnknownSourceError,
)
from shelfhold.library.domain.rendition import (
    LOCAL_PROCESSOR,
    Processor,
    RemoteProcessorRefusedError,
    RenditionParams,
    UnsupportedSourceError,
)
from tests import page_formats_env as pf
from tests.export_env import ExportEnv, export_env
from tests.fakes_clock import ManualClock
from tests.null_tombstones import NONE_PURGED
from tests.purge_env import ADMIN, TITLE, make_purge_env, spread

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
needs_avif = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF codec")
JPEG = RenditionParams(kind="jpeg")
PNG = RenditionParams(kind="png")


class Counting:
    """The real converter (the worker), counting how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0
        self.caps: list[ImageCaps] = []

    def __call__(self, data: bytes, params: RenditionParams, caps: ImageCaps) -> Reencoded:
        self.calls += 1
        self.caps.append(caps)
        return reencode_image(data, fmt=params.kind, quality=params.quality, caps=caps)


def _service(env: ExportEnv, convert: Counting | Callable[..., Reencoded] | None = None,
             clock: Clock | None = None, tombstones: SourceTombstones | None = None,
             ) -> RenditionService:  # fmt: skip
    return RenditionService(
        env.engine,
        env.lib.data,
        clock or ManualClock(NOW),
        tombstones=tombstones or NONE_PURGED,
        convert=convert,
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


def _decode(data: bytes) -> tuple[str | None, tuple[int, int]]:
    with Image.open(io.BytesIO(data)) as image:
        return image.format, image.size


@pytest.mark.req("EF-BIB-015")
@pytest.mark.parametrize("kind", ["jpeg", "png"])
def test_renditions_kepub_and_image_conversion_webp(kind: str) -> None:
    """The image volet: a client without WebP is served a JPEG or a PNG, made once and kept."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        service = _service(env, counting)
        params = RenditionParams(kind=kind, entry="1.webp")

        made = service.render(_content(env, unit), pf.webp(), params)
        again = service.render(_content(env, unit), pf.webp(), params)

        assert made == again and counting.calls == 1  # the second ask is the cache
        assert made.kind == kind and made.media_type == f"image/{kind}"
        data = service.read(made)
        assert data is not None and len(data) == made.size
        assert _decode(data) == (kind.upper(), (64, 48))
        (row,) = _rows(env)
        assert row.content_hash == made.content_hash and row.kind == kind
        (file,) = _files(env)
        assert file.name == f"{made.content_hash.removeprefix('sha256:')}.{kind}"  # named by hash


@pytest.mark.req("EF-BIB-015")
@needs_avif
def test_renditions_kepub_and_image_conversion_avif_to_jpeg() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        service = _service(env)
        made = service.render(_content(env, unit), pf.avif(), RenditionParams(kind="jpeg"))
        data = service.read(made)
        assert data is not None and _decode(data) == ("JPEG", (64, 48))


@pytest.mark.req("EF-BIB-015")
def test_a_rendition_is_checked_when_it_is_read() -> None:
    """A file altered on disk is never served: the hash is the name, so it is checked."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        service = _service(env)
        made = service.render(_content(env, unit), pf.webp(), JPEG)
        (file,) = _files(env)
        file.write_bytes(b"not what was made")
        assert service.read(made) is None
        assert service.cached(_content(env, unit), JPEG, pf.webp()) is None  # and it is made again
        assert service.render(_content(env, unit), pf.webp(), JPEG) == made


@pytest.mark.req("EF-BIB-015")
def test_a_format_with_no_decoder_is_refused_typed_and_never_converted() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        with pytest.raises(UnsupportedSourceError) as refused:
            _service(env, counting).render(_content(env, unit), pf.JXL_SIGNATURE, JPEG)
        assert refused.value.code == "rendition.format_unsupported"
        assert counting.calls == 0 and _rows(env) == [] and _files(env) == []


@pytest.mark.req("EF-BIB-015")
def test_a_rendition_of_an_unknown_source_is_refused() -> None:
    with export_env() as env:
        with pytest.raises(UnknownSourceError):
            _service(env).render("01HZZZZZZZZZZZZZZZZZZZZZZZ", pf.webp(), JPEG)
        assert _rows(env) == []


def _webp_x(width: int, height: int) -> bytes:
    canvas = (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    body = b"WEBPVP8X" + struct.pack("<I", 10) + b"\0\0\0\0" + canvas
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _avif_x(width: int, height: int) -> bytes:
    ispe = _box(b"ispe", b"\0\0\0\0" + struct.pack(">II", width, height))
    meta = _box(b"meta", b"\0\0\0\0" + _box(b"iprp", _box(b"ipco", ispe)))
    return _box(b"ftyp", b"avif\0\0\0\0avifmif1") + meta


@pytest.mark.req("EF-BIB-015", "ENF-SEC-003")
@pytest.mark.parametrize("bomb", [_webp_x(65_535, 65_535), _avif_x(100_000, 100_000)])
def test_rendition_decode_bomb_refused(bomb: bytes) -> None:
    """A few hundred bytes that announce billions of pixels are refused on the header, typed,
    before any worker starts: nothing is decoded, written or recorded."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        with pytest.raises(ImageRejected) as refused:
            _service(env, counting).render(_content(env, unit), bomb, JPEG)
        assert (refused.value.code, refused.value.stage) == ("image.too_large", "header")
        assert counting.calls == 0 and _rows(env) == [] and _files(env) == []


@pytest.mark.req("EF-BIB-015", "ENF-SEC-003")
def test_rendition_caps_are_the_callers_to_lower_never_to_raise_past_the_worker() -> None:
    """A real image over a lowered pixel cap is refused on its header; a source over the byte cap
    is refused at the door; neither reaches the worker."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        service = _service(env, counting)
        wide = io.BytesIO()
        Image.new("RGB", (2_000, 2_000), (9, 9, 9)).save(wide, "WEBP")
        with pytest.raises(ImageRejected) as pixels:
            service.render(_content(env, unit), wide.getvalue(), JPEG, max_pixels=1_000_000)
        assert (pixels.value.code, pixels.value.stage) == ("image.too_large", "header")
        with pytest.raises(ImageRejected) as heavy:
            service.render(_content(env, unit), b"x" * 2048, JPEG, max_bytes=1024)
        assert (heavy.value.code, heavy.value.stage) == ("image.too_large", "input")
        assert counting.calls == 0 and _rows(env) == [] and _files(env) == []
        # a render that passes is given the lowered caps, WebP and AVIF only, to take to the worker
        service.render(_content(env, unit), pf.webp(), JPEG, max_pixels=100_000)
        (given,) = counting.caps
        assert given.max_pixels == 100_000 and given.formats == ("WEBP", "AVIF")
        with pytest.raises(ImageRejected):  # zero is a cap of zero, not "no cap"
            service.render(_content(env, unit), pf.webp(), PNG, max_pixels=0)


@pytest.mark.req("EF-BIB-015")
def test_a_worker_that_fails_leaves_nothing_behind() -> None:
    def broken(data: bytes, params: RenditionParams, caps: ImageCaps) -> Reencoded:
        raise ImageRejected("image.worker_failed", "cannot start", "worker")

    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        with pytest.raises(ImageRejected):
            _service(env, broken).render(_content(env, unit), pf.webp(), JPEG)
        assert _rows(env) == [] and _files(env) == []


@pytest.mark.req("EF-BIB-015", "ENF-LEG-009")
def test_remote_processor_refuses_watermarked() -> None:
    remote = Processor("translator", egress="externe")
    with export_env() as env:
        marked = env.add_chapter(env.new_series(TITLE), "1", watermarked=True)
        counting = Counting()
        service = _service(env, counting)

        with pytest.raises(RemoteProcessorRefusedError) as refused:
            service.render(_content(env, marked), pf.webp(), JPEG, processor=remote)
        assert refused.value.code == "rendition.remote_refused_watermarked"
        assert counting.calls == 0 and _rows(env) == []  # refused before the bytes went anywhere

        # a local conversion of a watermarked file is fine: it stays on the host
        service.render(_content(env, marked), pf.webp(), JPEG, processor=LOCAL_PROCESSOR)
        assert len(_rows(env)) == 1


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_rendition_purged_with_source() -> None:
    """`purge --now` of a Unit takes its renditions, rows and files, and only those (ADR-0048)."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        other = env.add_chapter(series, "2")
        service = _service(env)
        theirs = service.render(_content(env, unit), pf.webp(), RenditionParams(kind="jpeg"))
        service.render(_content(env, unit), pf.webp(), RenditionParams(kind="png"))
        kept = service.render(
            _content(env, other), pf.webp(colour=(1, 2, 3)), RenditionParams(kind="jpeg")
        )
        assert len(_rows(env)) == 3 and len(_files(env)) == 3

        report = purge.purge.purge_unit(unit, now=purge.clock.now(), principal=ADMIN, typed=unit)

        assert report.complete
        remaining = _rows(env)
        assert [r.content_hash for r in remaining] == [kept.content_hash]
        assert [f.name.split(".")[0] for f in _files(env)] == [
            kept.content_hash.removeprefix("sha256:")
        ]
        assert theirs.content_hash not in {r.content_hash for r in remaining}


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_a_rendition_file_two_sources_share_goes_when_the_last_one_does() -> None:
    """Two sources that convert to the same bytes share one file, named by its hash: purging the
    first keeps the file for the second."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        other = env.add_chapter(series, "2")
        service = _service(env)
        a = service.render(_content(env, unit), pf.webp(), JPEG)
        b = service.render(_content(env, other), pf.webp(), JPEG)
        assert a.content_hash == b.content_hash and len(_files(env)) == 1 and len(_rows(env)) == 2

        purge.purge.purge_unit(unit, now=purge.clock.now(), principal=ADMIN, typed=unit)

        assert len(_rows(env)) == 1 and len(_files(env)) == 1
        data = service.read(b)
        assert data is not None and _decode(data)[0] == "JPEG"


@pytest.mark.req("EF-BIB-015")
def test_a_rendition_row_cannot_outlive_a_malformed_name() -> None:
    """The ledger only holds hashes and closed kinds: a path or a kind off the list is refused."""
    with export_env() as env, write_transaction(env.engine) as connection:
        for bad in (
            {"source_content_hash": "../x", "kind": "jpeg"},
            {"source_content_hash": "sha256:" + "a" * 64, "kind": "gif"},
            {"source_content_hash": "sha256:" + "a" * 64, "kind": "jpeg", "size": 0},
        ):
            values = {"content_hash": "sha256:" + "b" * 64, "size": 10, "params": "{}"} | bad
            with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO rendition (source_content_hash, kind, params, "
                        "content_hash, size) VALUES (:source_content_hash, :kind, :params, "
                        ":content_hash, :size)"
                    ),
                    values,
                )


@pytest.mark.req("EF-BIB-015")
def test_the_cache_is_keyed_by_the_bytes_that_were_converted() -> None:
    """Other bytes handed for the same content never get the answer made for the first."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        service = _service(env, counting)
        one = service.render(_content(env, unit), pf.webp((200, 0, 0)), JPEG)
        two = service.render(_content(env, unit), pf.webp((0, 200, 0)), JPEG)
        assert one.content_hash != two.content_hash and counting.calls == 2
        assert len(_rows(env)) == 2


@pytest.mark.req("EF-BIB-015")
def test_a_failed_record_leaves_no_file_and_a_purged_source_is_not_recorded() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        service = _service(env)
        content = _content(env, unit)

        def fail(rendition: object, origin: object) -> None:
            raise RuntimeError("database is locked")

        original = service._record
        service._record = fail  # type: ignore[method-assign,assignment]
        with pytest.raises(RuntimeError):
            service.render(content, pf.webp(), JPEG)
        assert _files(env) == [] and _rows(env) == []
        service._record = original  # type: ignore[method-assign]

        def purge_meanwhile(data: bytes, params: RenditionParams, caps: ImageCaps) -> Reencoded:
            made = reencode_image(data, fmt=params.kind, quality=params.quality, caps=caps)
            with write_transaction(env.engine) as connection:  # the source goes during the work
                connection.execute(
                    sa.update(storage.content)
                    .where(storage.content.c.id == content)
                    .values(original_sha256="sha256:" + "9" * 64)
                )
            return made

        racing = _service(env, purge_meanwhile)
        with pytest.raises(UnknownSourceError):
            racing.render(content, pf.webp(), PNG)
        assert _files(env) == [] and _rows(env) == []


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_orphans_are_collected_after_minutes_and_named_files_never() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        clock = ManualClock(NOW)
        service = _service(env, clock=clock)
        made = service.render(_content(env, unit), pf.webp(), JPEG)
        folder = env.lib.data / "cache" / "renditions" / "ab"
        folder.mkdir(parents=True)
        stray = folder / ("c" * 64 + ".png")
        stray.write_bytes(b"left by a crash")
        odd = folder / "notes.txt"
        odd.write_bytes(b"not ours")
        os.utime(stray, (NOW.timestamp(), NOW.timestamp()))
        assert service.collect_orphans() == 0  # younger than the grace by the clock
        old = NOW.timestamp() - 11 * 60  # minutes, not a day: a render takes seconds
        os.utime(stray, (old, old))
        assert service.collect_orphans() == 1
        assert not stray.exists() and odd.exists()  # only a name this module writes
        assert service.read(made) is not None  # the file a row names stays


class _Tombstoned:
    def __init__(self) -> None:
        self.asked = 0

    def check_content(self, hashes: object = ()) -> bool:
        self.asked += 1
        return True

    def has_values(self, candidate: object) -> bool:
        self.asked += 1
        return True


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_nothing_is_made_from_a_file_whose_purge_has_begun() -> None:
    """The tombstone comes first in a purge: a render started or finished after it is refused,
    its file removed and no row written."""
    from shelfhold.library.domain.failures import TombstonedContentError

    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        with pytest.raises(TombstonedContentError):
            _service(env, counting, tombstones=_Tombstoned()).render(
                _content(env, unit), pf.webp(), JPEG
            )
        assert counting.calls == 0 and _rows(env) == [] and _files(env) == []

        class Late:  # the purge records its tombstone while the worker runs
            on = False

            def check_content(self, hashes: object = ()) -> bool:
                return self.on

            def has_values(self, candidate: object) -> bool:
                return self.on

        late = Late()

        def convert(data: bytes, params: RenditionParams, caps: ImageCaps) -> Reencoded:
            made = reencode_image(data, fmt=params.kind, quality=params.quality, caps=caps)
            late.on = True
            return made

        with pytest.raises(TombstonedContentError):
            _service(env, convert, tombstones=late).render(_content(env, unit), pf.webp(), JPEG)
        assert _rows(env) == [] and _files(env) == []


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_the_orphan_sweep_is_part_of_the_window_job_of_the_cache() -> None:
    from types import SimpleNamespace

    from shelfhold.jobs.thumbnail_gc import THUMBNAIL_GC_KIND, thumbnail_gc_handler
    from tests.integration.test_thumbnails import context

    with export_env() as env:
        folder = env.lib.data / "cache" / "renditions" / "ab"
        folder.mkdir(parents=True)
        stray = folder / ("d" * 64 + ".jpeg")
        stray.write_bytes(b"left by a race")
        old = datetime.now(UTC).timestamp() - 3600
        os.utime(stray, (old, old))
        comp = SimpleNamespace(engine=env.engine, settings=SimpleNamespace(data_dir=env.lib.data))
        ctx = context(THUMBNAIL_GC_KIND)
        thumbnail_gc_handler(comp)(ctx)  # type: ignore[arg-type]
        assert ctx.counters["renditions"] == 1 and not stray.exists()


@pytest.mark.req("EF-BIB-015", "ENF-SEC-003")
def test_an_avif_whose_header_cannot_be_walked_is_refused_before_the_worker() -> None:
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        no_meta = _box(b"ftyp", b"avif\0\0\0\0avifmif1") + _box(b"free", b"\0" * 64)
        with pytest.raises(ImageRejected) as refused:
            _service(env, counting).render(_content(env, unit), no_meta, JPEG)
        assert (refused.value.code, refused.value.stage) == ("image.format_refused", "header")
        assert counting.calls == 0 and _rows(env) == []
