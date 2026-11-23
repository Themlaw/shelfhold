"""Relecture adverse de M6 L12 (EF-BIB-018): what the sweep of the derived caches must still hold.

Each test is a scenario the first version gets wrong: a folder erased as a tree by the one eraser
of the caches, a KEPUB found in the cache and evicted before its read, a rendition file that two
rows share erased for the one that is kept, the bodies of the HTTP cache in a sub-folder never
counted, and the pin of an in-flight read that the first test never put to work."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import metadata, platform, storage
from shelfhold.library.application import renditions as renditions_module
from shelfhold.library.application.cache_quotas import (
    CachePolicy,
    erase_cache_path,
    purge_caches,
)
from shelfhold.library.application.kepub import KepubError
from shelfhold.library.application.renditions import Rendition, RenditionService
from tests.export_env import export_env
from tests.fakes_clock import ManualClock
from tests.null_tombstones import NONE_PURGED
from tests.purge_env import TITLE
from tests.unit.test_kepub import book, epub

NOW = datetime(2026, 11, 24, 5, 10, tzinfo=UTC)
MIB = 1024 * 1024
DAY = 86400


def _hex(seed: str) -> str:
    return sha256(seed.encode()).hexdigest()


def _policy(**over: int) -> CachePolicy:
    values: dict[str, int] = {
        "thumbnails_quota_bytes": 256 * MIB,
        "thumbnails_retention_days": 180,
        "renditions_quota_bytes": 1024 * MIB,
        "renditions_retention_days": 90,
        "http_quota_bytes": 64 * MIB,
        "http_retention_days": 30,
        "metadata_rows": 4096,
    }
    values.update(over)
    return CachePolicy(**values)


def _write(root: Path, rel: str, body: bytes, *, age_days: float = 1) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    stamp = NOW.timestamp() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def data(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    return folder


def _rendition_row(
    engine: Engine, out: str, *, source_seed: str, size: int, age_days: float, kind: str = "jpeg"
) -> Rendition:
    source = f"sha256:{_hex(source_seed)}"
    with engine.begin() as connection:
        connection.execute(
            sa.insert(storage.rendition).values(
                source_content_hash=source,
                kind=kind,
                params={"src": source_seed},
                content_hash=f"sha256:{out}",
                size=size,
                created_at=NOW,
            )
        )
    return Rendition(source, kind, f'{{"src": "{source_seed}"}}', f"sha256:{out}", size)


# -- the one eraser of the caches erases files, never a folder tree ------------------------------


@pytest.mark.req("EF-BIB-018")
def test_cache_eraser_never_removes_a_folder_tree(engine: Engine, data: Path) -> None:
    """`erase_cache_path` erases one file of a cache. A folder at a cache name (a mount point, a
    folder an operator left there, a body ref that names a folder) is not a cache file: it is
    refused, and what it holds stays. `erase_data_path` removes a folder as a tree, crossing
    any mount point inside it (no `st_dev` check), so the allowlist must refuse it first."""
    out = _hex("a-folder-at-a-rendition-name")
    rendition_dir = data / f"cache/renditions/{out[:2]}/{out}.jpeg"
    kept_in_rendition_dir = _write(data, f"{rendition_dir.relative_to(data)}/kept.cbz", b"kept")
    http_dir = data / "cache/http/mounted"
    kept_in_http_dir = _write(data, "cache/http/mounted/works/content.cbz", b"the kept copy")

    for rel in (str(rendition_dir.relative_to(data)), "cache/http/mounted"):
        with contextlib.suppress(ValueError):  # a refusal is the right answer
            erase_cache_path(data, rel)
    assert kept_in_rendition_dir.exists(), "the eraser removed a folder tree at a rendition name"
    assert kept_in_http_dir.exists(), "the eraser removed a folder tree under the HTTP cache"

    # and through the sweep: a row of each kind, past its retention, naming a folder
    _rendition_row(engine, out, source_seed="dir", size=10, age_days=200)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.http_cache).values(
                scope="source",
                url_hash=_hex("mounted"),
                stored_at=NOW,
                max_age=1,
                body_ref="cache/http/mounted",
            )
        )
    os.utime(rendition_dir, (NOW.timestamp() - 200 * DAY,) * 2)
    with contextlib.suppress(ValueError):
        purge_caches(
            engine, data, ManualClock(NOW), _policy(renditions_quota_bytes=1, http_quota_bytes=1)
        )
    assert kept_in_rendition_dir.exists() and rendition_dir.is_dir()
    assert kept_in_http_dir.exists() and http_dir.is_dir()


# -- a KEPUB found in the cache is not evicted before it is read --------------------------------


@pytest.mark.req("EF-BIB-018")
def test_a_kepub_found_in_the_cache_is_pinned_until_it_is_read() -> None:
    """`kepub_bytes` looks the KEPUB up (`render_kepub`, a cache hit) and only then reads it: the
    pin is taken by `read`, so a sweep that runs between the two (a quota under pressure) evicts
    the file the request is about to serve, and the reader gets `unavailable` (a 404 at the Kobo
    surface). The pin must cover the request from the lookup to the read."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        with env.engine.connect() as connection:
            content_id = str(
                connection.execute(
                    sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit)
                ).scalar_one()
            )
        service = RenditionService(
            env.engine, env.lib.data, ManualClock(NOW), tombstones=NONE_PURGED
        )
        source = epub(book())
        made = service.render_kepub(content_id, source)  # in the cache now
        service.unpin(made)  # handed back pinned since the fix: this test holds no request
        expected = service.read(made)
        assert expected is not None

        lookup = service.render_kepub
        swept: list[bool] = []

        def lookup_then_sweep(cid: str, data: bytes) -> Rendition:
            found = lookup(cid, data)
            if not swept:  # the daily sweep runs once, right after the hit
                swept.append(True)
                purge_caches(
                    env.engine,
                    env.lib.data,
                    ManualClock(NOW),
                    _policy(renditions_quota_bytes=1),
                    pinned=RenditionService.pinned(),
                )
            return found

        service.render_kepub = lookup_then_sweep  # type: ignore[method-assign]
        try:
            assert service.kepub_bytes(content_id, source) == expected
        except KepubError as error:
            pytest.fail(f"the KEPUB a request found in the cache was evicted under it: {error}")


# -- a rendition file shared by two rows ---------------------------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_a_shared_rendition_file_is_counted_once_and_kept_for_its_other_row(
    engine: Engine, data: Path
) -> None:
    """Two sources whose page converts to the same bytes share one file (`_drop_if_unreferenced`:
    a file another row still names is that row's). The sweep sums each row's size, so the disk
    is counted twice and the quota evicts a file the disk had room for; and it erases the file
    while the other row, kept, still names it."""
    body = b"r" * 1000
    out = sha256(body).hexdigest()
    _write(data, f"cache/renditions/{out[:2]}/{out}.jpeg", body, age_days=1)
    _rendition_row(engine, out, source_seed="chapter-1", size=1000, age_days=1)
    _rendition_row(engine, out, source_seed="chapter-2", size=1000, age_days=1)

    purge_caches(engine, data, ManualClock(NOW), _policy(renditions_quota_bytes=1500))

    with engine.connect() as connection:
        rows = connection.execute(sa.select(storage.rendition.c.content_hash)).scalars().all()
    for content_hash in rows:  # every row kept names a file that is there
        digest = content_hash.removeprefix("sha256:")
        assert (data / f"cache/renditions/{digest[:2]}/{digest}.jpeg").exists()
    assert (data / f"cache/renditions/{out[:2]}/{out}.jpeg").exists(), (
        "1000 bytes on disk under a 1500-byte quota, evicted all the same"
    )


# -- the bodies of the HTTP cache in a sub-folder ------------------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_http_bodies_in_a_sub_folder_count_towards_the_quota(engine: Engine, data: Path) -> None:
    """The allowlist admits `cache/http/<a>/<b>` (any depth), but the sizing asks `stat_of` for a
    name with a slash, which answers None: such a body weighs 0 and the quota never evicts it."""
    with engine.begin() as connection:
        for name, age in (("older", 2), ("newer", 1)):
            rel = f"cache/http/source/{name}"
            _write(data, rel, b"h" * 512, age_days=age)
            connection.execute(
                sa.insert(platform.http_cache).values(
                    scope="source",
                    url_hash=_hex(name),
                    stored_at=NOW.replace(day=NOW.day - age),
                    max_age=400 * DAY,
                    body_ref=rel,
                )
            )

    purge_caches(engine, data, ManualClock(NOW), _policy(http_quota_bytes=600))

    assert not (data / "cache/http/source/older").exists(), "1024 bytes under a 600-byte quota"
    assert (data / "cache/http/source/newer").exists()


# -- the pin of an in-flight read, put to work ---------------------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_a_pinned_rendition_stays_even_when_the_quota_wants_it(engine: Engine, data: Path) -> None:
    """The first test pins `served` under a 1500-byte quota: once `older` is gone the 1000 bytes
    left fit, so `served` would have stayed without the pin. Under 500 bytes the quota does want
    it, and only the pin keeps it (green on the PR: it hardens the test, it shows no defect)."""
    service = RenditionService(engine, data, ManualClock(NOW), tombstones=NONE_PURGED)
    older_out, served_out = _hex("older"), _hex("served")
    _write(data, f"cache/renditions/{older_out[:2]}/{older_out}.jpeg", b"r" * 64, age_days=10)
    _write(data, f"cache/renditions/{served_out[:2]}/{served_out}.jpeg", b"r" * 64, age_days=1)
    older = _rendition_row(engine, older_out, source_seed="older", size=1000, age_days=10)
    served = _rendition_row(engine, served_out, source_seed="served", size=1000, age_days=1)

    with service.serving(served):
        purge_caches(
            engine,
            data,
            ManualClock(NOW),
            _policy(renditions_quota_bytes=500),
            pinned=RenditionService.pinned(),
        )
        assert not (data / older.rel).exists()
        assert (data / served.rel).exists()  # over the quota, kept by the pin alone


# -- second pass of the review: the fixes of the sweep ------------------------------------------


def _bytes_under(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


@pytest.mark.req("EF-BIB-018")
def test_the_low_disk_guard_evicts_only_until_the_volume_is_back_above(
    engine: Engine, data: Path
) -> None:
    """While the low-disk guard says `/data` is under its mark, the sweep evicts "until the
    volume is back above". The guard reads the disk afresh, but the files are erased only once
    the eviction loop is over: inside the loop the guard can never change its answer, so one
    moment of pressure empties the whole cache. Here, two of three files are enough."""
    for seed, age in (("oldest", 3), ("middle", 2), ("newest", 1)):
        body = b"r" * 1000
        out = _hex(seed)
        _write(data, f"cache/renditions/{out[:2]}/{out}.jpeg", body, age_days=age)
        _rendition_row(engine, out, source_seed=seed, size=1000, age_days=age)

    def pressure() -> bool:  # the volume is under its mark while more than 1500 bytes are used
        return _bytes_under(data) > 1500

    purge_caches(engine, data, ManualClock(NOW), _policy(), pressure=pressure)

    newest = _hex("newest")
    assert (data / f"cache/renditions/{newest[:2]}/{newest}.jpeg").exists(), (
        "the guard emptied the whole cache: 1000 bytes would have put the volume back above"
    )
    assert _bytes_under(data) == 1000


@pytest.mark.req("EF-BIB-018")
def test_a_cache_hit_is_pinned_before_its_check_read_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_cached` checks the file with `read` (pinned for its own length), lets the pin go, and
    only then takes the hold it hands back: a sweep that runs between the two finds no pin and
    takes the file the request is about to read. The hold must be taken before the check read
    lets its own pin go."""
    monkeypatch.setattr(renditions_module, "_PINNED", {})  # no pin left over by another test
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        with env.engine.connect() as connection:
            content_id = str(
                connection.execute(
                    sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit)
                ).scalar_one()
            )
        service = RenditionService(
            env.engine, env.lib.data, ManualClock(NOW), tombstones=NONE_PURGED
        )
        source = epub(book())
        made = service.render_kepub(content_id, source)
        service.unpin(made)
        expected = service.read(made)
        assert expected is not None and RenditionService.pinned() == frozenset()

        real_read = service.read
        swept: list[bool] = []

        def read_then_sweep(rendition: Rendition) -> bytes | None:
            found = real_read(rendition)
            if not swept:  # the sweep lands right after the check read of the lookup
                swept.append(True)
                purge_caches(
                    env.engine,
                    env.lib.data,
                    ManualClock(NOW),
                    _policy(renditions_quota_bytes=1),
                    pinned=RenditionService.pinned(),
                )
            return found

        service.read = read_then_sweep  # type: ignore[method-assign]
        try:
            assert service.kepub_bytes(content_id, source) == expected
        except KepubError as error:
            pytest.fail(f"the cache hit was evicted before its hold was taken: {error}")


@pytest.mark.req("EF-BIB-018")
def test_a_thumbnail_row_whose_shard_folder_is_gone_is_dropped(engine: Engine, data: Path) -> None:
    """`_unlink` turns a folder missing on the way (`FileNotFoundError`) into a `ValueError`, and
    the thumbnail sweep keeps the row of a refused path: a row whose shard folder is gone (its
    files removed by hand, a restore without the cache) stays for ever, warned about every day,
    and counted in the use that `status` and Health show. A missing path is nothing to erase,
    not a refusal: the row goes."""
    digest = _hex("no-shard")
    with engine.begin() as connection:
        connection.execute(
            sa.insert(storage.thumbnail).values(
                content_hash=f"sha256:{digest}",
                size=160,
                path=f"{digest[:2]}/{digest}-160.webp",
                bytes=1024,
                created_at=NOW.replace(year=NOW.year - 2),
                last_used_at=NOW.replace(year=NOW.year - 2),
            )
        )
    assert not (data / "thumbnails").exists()

    purge_caches(engine, data, ManualClock(NOW), _policy())

    with engine.connect() as connection:
        left = connection.execute(sa.select(sa.func.count()).select_from(storage.thumbnail))
        assert left.scalar_one() == 0, "a thumbnail row past its retention, with no file, stayed"
