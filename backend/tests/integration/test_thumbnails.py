"""M3 L24 (EF-BIB-020, ENF-PERF-010, ENF-SEC-003): cover thumbnails, the service and its job.

160, 320 and 640 pixels, WebP, addressed by the hash of the cover they come from, made on first
use by the decode worker (never by the request's own process) and caught up in the window; the
orphans go in the window and nowhere else, and only from the thumbnail folder.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image

from shelfhold.config import Settings
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.jobs.public import DEFAULT_WINDOW
from shelfhold.jobs.thumbnail_gc import (
    THUMBNAIL_CATCHUP_KIND,
    THUMBNAIL_GC_KIND,
    register_thumbnail_jobs,
    thumbnail_catchup_handler,
    thumbnail_gc_handler,
)
from shelfhold.jobs.window import window_rank
from shelfhold.library.application.thumbnails import (
    SIZES,
    ThumbnailService,
    cover_hash,
)
from tests.auth_env import ManualClock
from tests.l41_env import context
from tests.thumbnail_env import put_cover, thumbnail_files, webp_bytes
from tests.visibility_env import seed_database

LIMITS = WorkerLimits(network_namespace=False)


class Rig:
    def __init__(self, settings: Settings) -> None:
        migrate(settings)
        self.settings = settings
        self.clock = ManualClock()
        self.seed = seed_database(settings.database_path)
        self.engine = self.seed.engine
        self.service = ThumbnailService(self.engine, settings.data_dir, self.clock, limits=LIMITS)
        self.data = settings.data_dir

    def cover(self, key: str, colour: tuple[int, int, int]) -> str:
        with self.engine.begin() as connection:
            return put_cover(self.data, connection, self.seed.editions[key], colour)

    def rows(self) -> list[Any]:
        with self.engine.connect() as connection:
            return list(connection.execute(sa.select(storage.thumbnail)).mappings())

    def referenced(self) -> set[str]:
        with self.engine.connect() as connection:
            values = connection.execute(
                sa.select(catalog.metadata_field.c.value).where(
                    catalog.metadata_field.c.field == "cover"
                )
            ).scalars()
            return {cover_hash(v.strip('"')) or "" for v in values}


@pytest.fixture
def rig(settings: Settings) -> Iterator[Rig]:
    made = Rig(settings)
    yield made
    made.engine.dispose()


@pytest.mark.req("EF-BIB-020")
def test_thumbnails_160_320_640_webp_hash_addressed(rig: Rig) -> None:
    digest = rig.cover("ok", (200, 30, 30))
    assert SIZES == (160, 320, 640)
    for size in SIZES:
        thumb = rig.service.get(digest, size)
        assert thumb is not None and thumb.size == size
        assert thumb.data[:4] == b"RIFF" and thumb.data[8:12] == b"WEBP"
        image = Image.open(io.BytesIO(thumb.data))
        assert image.format == "WEBP" and max(image.size) == size  # 900x1200: the height is 'size'
        assert image.size[0] < image.size[1]  # the ratio is kept
    files = thumbnail_files(rig.data)
    assert len(files) == 3 and all(digest in p.name for p in files)  # addressed by the cover hash
    assert {r["size"] for r in rig.rows()} == {160, 320, 640}
    assert all(r["content_hash"] == f"sha256:{digest}" for r in rig.rows())
    assert all(
        r["bytes"] == (rig.data / "thumbnails" / r["path"]).stat().st_size for r in rig.rows()
    )


@pytest.mark.req("EF-BIB-020")
def test_thumbnail_is_made_once_then_served_from_disk(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.library.application import thumbnails as module

    digest = rig.cover("ok", (10, 200, 30))
    first = rig.service.get(digest, 320)
    calls: list[int] = []
    real = module.make_thumbnail
    monkeypatch.setattr(module, "make_thumbnail", lambda *a, **k: calls.append(1) or real(*a, **k))
    again = rig.service.get(digest, 320)
    assert calls == [] and again is not None and first is not None
    assert (again.data, again.etag) == (first.data, first.etag)
    assert len(rig.rows()) == 1


@pytest.mark.req("EF-BIB-020")
def test_two_editions_with_the_same_cover_share_one_set_of_thumbnails(rig: Rig) -> None:
    one = rig.cover("ok", (5, 5, 200))
    two = rig.cover("twelve", (5, 5, 200))
    assert one == two
    rig.service.get(one, 160)
    assert len(thumbnail_files(rig.data)) == 1


@pytest.mark.req("EF-BIB-020", "ENF-SEC-003")
def test_thumbnail_refuses_what_is_not_a_hash_a_size_or_the_cover_it_names(rig: Rig) -> None:
    digest = rig.cover("ok", (90, 90, 90))
    for bad in ("", "../etc/passwd", digest.upper(), digest[:-1], "g" * 64, digest + "0"):
        assert rig.service.get(bad, 160) is None, bad
    for size in (0, 100, 161, 641, 1280, -160):
        assert rig.service.get(digest, size) is None, size
    # a cover file whose bytes are not the ones its name says: never decoded, never served
    (rig.data / "covers" / f"{digest}.webp").write_bytes(webp_bytes((1, 2, 3)))
    assert rig.service.get(digest, 160) is None
    assert thumbnail_files(rig.data) == [] and rig.rows() == []
    # a cover that is not there
    assert rig.service.get("a" * 64, 160) is None


@pytest.mark.req("EF-BIB-020", "ENF-SEC-003")
def test_thumbnail_of_an_undecodable_cover_is_none_and_leaves_nothing(rig: Rig) -> None:
    import hashlib

    junk = b"not an image at all"
    digest = hashlib.sha256(junk).hexdigest()
    (rig.data / "covers").mkdir(parents=True, exist_ok=True)
    (rig.data / "covers" / f"{digest}.webp").write_bytes(junk)
    assert rig.service.get(digest, 160) is None
    assert thumbnail_files(rig.data) == [] and rig.rows() == []


@pytest.mark.req("EF-BIB-020")
def test_thumbnail_cover_hash_reads_only_the_store_form() -> None:
    digest = "0123456789abcdef" * 4
    assert cover_hash(f"covers/{digest}.webp") == digest
    for bad in (
        digest,
        f"covers/{digest}.png",
        f"covers/../{digest}.webp",
        f"/covers/{digest}.webp",
        f"covers/{digest[:-1]}.webp",
        f"covers/{digest.upper()}.webp",
        "",
        "covers/.webp",
        f"covers/x/{digest}.webp",
    ):
        assert cover_hash(bad) is None, bad


@pytest.mark.req("EF-BIB-020")
def test_catch_up_makes_the_three_sizes_of_every_referenced_cover_and_stops_when_closing(
    rig: Rig,
) -> None:
    a = rig.cover("ok", (1, 100, 100))
    b = rig.cover("twelve", (2, 100, 100))
    rig.service.get(a, 160)  # already there: not made again
    stop_after: list[int] = []

    def closing() -> bool:
        return len(stop_after) >= 4

    class Spy(ThumbnailService):
        def _make(self, *args: Any, **kwargs: Any) -> Any:
            stop_after.append(1)
            return super()._make(*args, **kwargs)

    spy = Spy(rig.engine, rig.data, rig.clock, limits=LIMITS)
    assert spy.catch_up([a, b], closing=closing) == 4  # stopped between two covers' sizes
    assert len(rig.rows()) == 5
    assert rig.service.catch_up([a, b], closing=lambda: False) == 1  # the one still missing
    assert len(rig.rows()) == 6 and rig.service.catch_up([a, b], closing=lambda: False) == 0


@pytest.mark.req("EF-BIB-020")
def test_orphan_thumbnails_collected_in_window(rig: Rig) -> None:
    keep = rig.cover("ok", (50, 0, 0))
    gone = rig.cover("twelve", (0, 50, 0))
    for digest in (keep, gone):
        rig.service.catch_up([digest], closing=lambda: False)
    assert len(rig.rows()) == 6
    with rig.engine.begin() as connection:  # the Edition no longer points at its cover
        connection.execute(
            sa.delete(catalog.metadata_field).where(
                catalog.metadata_field.c.entity_id == rig.seed.editions["twelve"]
            )
        )
    source = rig.data / "covers" / f"{gone}.webp"
    comp = SimpleNamespace(engine=rig.engine, settings=SimpleNamespace(data_dir=rig.data))
    ctx = context(THUMBNAIL_GC_KIND)
    thumbnail_gc_handler(comp, limits=LIMITS)(ctx)  # type: ignore[arg-type]
    assert ctx.counters["files"] == 3 and ctx.counters["rows"] == 3
    assert {r["content_hash"] for r in rig.rows()} == {f"sha256:{keep}"}
    assert all(keep in p.name for p in thumbnail_files(rig.data))
    assert source.exists()  # the cover itself is not the job's: only the thumbnails go


@pytest.mark.req("EF-BIB-020", "ENF-OPS-014")
def test_thumbnail_jobs_are_window_jobs_ranked_by_the_window() -> None:
    class Scheduler:
        def __init__(self) -> None:
            self.added: list[tuple[str, bool]] = []

        def add(self, kind: str, _cron: str, *, windowed: bool = False, **_: Any) -> None:
            self.added.append((kind, windowed))

    class Pools:
        def __init__(self) -> None:
            self.scheduler = Scheduler()
            self.window = DEFAULT_WINDOW
            self.registered: dict[str, dict[str, Any]] = {}

        def register(self, kind: str, **options: Any) -> None:
            self.registered[kind] = options

    pools = Pools()
    register_thumbnail_jobs(pools, SimpleNamespace())  # type: ignore[arg-type]
    for kind in (THUMBNAIL_GC_KIND, THUMBNAIL_CATCHUP_KIND):
        assert pools.registered[kind]["windowed"] is True  # never by day
        assert (kind, True) in pools.scheduler.added
    assert window_rank(THUMBNAIL_GC_KIND) < window_rank(THUMBNAIL_CATCHUP_KIND)  # collect first


@pytest.mark.req("EF-BIB-020")
def test_a_closing_window_stops_the_gc_and_the_catch_up(rig: Rig) -> None:
    digest = rig.cover("ok", (7, 7, 7))
    comp = SimpleNamespace(engine=rig.engine, settings=SimpleNamespace(data_dir=rig.data))
    ctx = context(THUMBNAIL_CATCHUP_KIND)
    ctx.window_end = ctx.clock.now()  # already closing
    thumbnail_catchup_handler(comp, limits=LIMITS)(ctx)  # type: ignore[arg-type]
    assert ctx.preempted is True and rig.rows() == [] and digest
    ctx = context(THUMBNAIL_CATCHUP_KIND)
    ctx.window_end = ctx.clock.now() + timedelta(hours=1)
    thumbnail_catchup_handler(comp, limits=LIMITS)(ctx)  # type: ignore[arg-type]
    assert ctx.counters["made"] == 3 and len(rig.rows()) == 3


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_gc_deletes_only_inside_the_thumbnail_folder(rig: Rig) -> None:
    """The row's path is data, not trust: a path out of the folder, a link, a folder, a name that
    is not a thumbnail's, are never removed; the cover and what sits beside it stay."""
    digest = rig.cover("ok", (30, 30, 30))
    rig.service.get(digest, 160)
    outside = rig.data / "elsewhere" / "precious.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("keep")
    folder = rig.data / "thumbnails"
    link = folder / "ab" / f"{'ab' * 32}-160.webp"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    other = rig.data / "covers" / "neighbour.webp"
    other.write_bytes(b"x")
    with rig.engine.begin() as connection:  # rows an attacker or a bug could have written
        connection.execute(sa.delete(catalog.metadata_field))  # all covers orphaned
        now = rig.clock.now()
        for hashed, rel in (("ab" * 32, "elsewhere/precious.txt"), ("cd" * 32, "ab")):
            connection.execute(
                sa.insert(storage.thumbnail).values(
                    content_hash=f"sha256:{hashed}",
                    size=160,
                    path=rel,
                    bytes=4,
                    created_at=now,
                )
            )
    rig.service.collect(set(), closing=lambda: False)
    assert outside.read_text() == "keep" and other.exists()
    assert (rig.data / "covers" / f"{digest}.webp").exists()
    assert rig.rows() == []  # the rows go; the unsafe targets were not touched
    assert not link.exists() or link.is_symlink()  # a link is removed as a link, never followed


@pytest.mark.req("EF-BIB-020")
def test_gc_removes_stray_files_after_a_grace_and_rows_without_a_file(rig: Rig) -> None:
    digest = rig.cover("ok", (60, 60, 60))
    rig.service.get(digest, 160)
    folder = rig.data / "thumbnails" / "zz"
    folder.mkdir(parents=True)
    old, young = folder / f"{'1' * 64}-160.webp", folder / f"{'2' * 64}-160.webp"
    old.write_bytes(b"x")
    young.write_bytes(b"x")
    before = (rig.clock.now() - timedelta(days=3)).timestamp()
    os.utime(old, (before, before))
    os.utime(young, None)
    (rig.data / "thumbnails" / "zz" / "notes.txt").write_text("not a thumbnail")
    kept = next(p for p in thumbnail_files(rig.data) if digest in p.name)
    kept.unlink()  # the row is left with no file: it is dropped, and made again on demand
    result = rig.service.collect({digest}, closing=lambda: False)
    assert not old.exists() and young.exists()  # a file just written may be a generation in flight
    assert (folder / "notes.txt").exists()  # a name that is not a thumbnail's is not ours
    assert result.files == 1 and rig.rows() == []
    assert rig.service.get(digest, 160) is not None  # made again
