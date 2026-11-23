"""L12 (EF-BIB-018, D17): retention and quotas of the derived caches.

Every cache the host creates is bounded: a quota in bytes (or in rows for what lives in the
database) and a retention in days, enforced by a daily sweep that evicts by last access (LRU).
The sweep is a system emitter of the closed list (passe 13, N2): it erases only paths the host
itself created — the cache folders under `/data`, by the name patterns their writers use — never
`/store`, `/library`, `.shelfhold/`, nor an original. A rendition served by an in-flight
`/content` ticket is pinned for the time of the read and is not evicted. Synthetic data only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.identity.public import DELETION_SITES, SYSTEM_EMITTERS
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import EMITTER_PERIMETERS, EmitterPerimeterError
from shelfhold.infrastructure.fsops.emitters import require_inside
from shelfhold.infrastructure.schema import metadata, platform, storage
from shelfhold.library.application.cache_quotas import (
    CACHE_PURGE_EMITTER,
    CachePolicy,
    cache_state,
    erase_cache_path,
    purge_caches,
)
from shelfhold.library.application.renditions import Rendition, RenditionService
from shelfhold.ops.health import build_health_page
from shelfhold.ops.services import OpsServices
from tests.fakes_clock import ManualClock
from tests.null_tombstones import NONE_PURGED

NOW = datetime(2026, 11, 24, 5, 10, tzinfo=UTC)
MIB = 1024 * 1024
DAY = 86400


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def data(tmp_path: Path) -> Iterator[Path]:
    folder = tmp_path / "data"
    folder.mkdir()
    yield folder


# -- the fixtures of each cache, as their writers leave them ------------------------------------


def _hex(seed: str) -> str:
    return sha256(seed.encode()).hexdigest()


def _write(root: Path, rel: str, body: bytes, *, age_days: float | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if age_days is not None:
        stamp = NOW.timestamp() - age_days * DAY
        os.utime(path, (stamp, stamp))
    return path


def _age(days: float) -> datetime:
    return NOW - timedelta(days=days)


def add_thumbnail(
    engine: Engine,
    data: Path,
    seed: str,
    *,
    used_age_days: float,
    size: int = 160,
    bytes_: int = 1024,
) -> str:
    digest = _hex(seed)
    _write(
        data, f"thumbnails/{digest[:2]}/{digest}-{size}.webp", b"t" * bytes_, age_days=used_age_days
    )
    with engine.begin() as connection:
        connection.execute(
            sa.insert(storage.thumbnail).values(
                content_hash=f"sha256:{digest}",
                size=size,
                path=f"{digest[:2]}/{digest}-{size}.webp",
                bytes=bytes_,
                created_at=_age(used_age_days),
                last_used_at=_age(used_age_days),
            )
        )
    return digest


def add_rendition(
    engine: Engine, data: Path, seed: str, *, age_days: float, size: int = 1000, kind: str = "jpeg"
) -> Rendition:
    out = _hex(seed)
    _write(data, f"cache/renditions/{out[:2]}/{out}.{kind}", b"r" * 64, age_days=age_days)
    source = f"sha256:{_hex(seed + '-src')}"
    with engine.begin() as connection:
        connection.execute(
            sa.insert(storage.rendition).values(
                source_content_hash=source,
                kind=kind,
                params={"src": out},
                content_hash=f"sha256:{out}",
                size=size,
                created_at=_age(age_days),
            )
        )
    return Rendition(source, kind, f'{{"src": "{out}"}}', f"sha256:{out}", size)


def add_http(
    engine: Engine,
    data: Path,
    name: str,
    *,
    age_days: float,
    max_age: int,
    body: bytes = b"h" * 512,
) -> None:
    rel = f"cache/http/{name}"
    _write(data, rel, body, age_days=age_days)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.http_cache).values(
                scope="source",
                url_hash=_hex(name),
                stored_at=_age(age_days),
                max_age=max_age,
                body_ref=rel,
            )
        )


def add_metadata(
    engine: Engine, key: str, *, fetched_age_days: float, expires_age_days: float
) -> None:
    """`expires_age_days` is positive when the expiry is already past (the row is expired)."""
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.metadata_cache).values(
                provider="openlibrary",
                key_hash=_hex(key),
                payload={"key": key},
                fetched_at=_age(fetched_age_days),
                expires_at=_age(expires_age_days),
            )
        )


def policy(**over: int) -> CachePolicy:
    values: dict[str, int] = {
        "thumbnails_quota_bytes": 2 * 1024**3,
        "thumbnails_retention_days": 90,
        "renditions_quota_bytes": 5 * 1024**3,
        "renditions_retention_days": 90,
        "http_quota_bytes": 512 * 1024**2,
        "http_retention_days": 90,
        "metadata_rows": 4096,
        "metadata_quota_bytes": 256 * 1024**2,
    }
    values.update(over)
    return CachePolicy(**values)


def _rows(engine: Engine, table: sa.Table) -> int:
    with engine.connect() as connection:
        return int(connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def _thumbnail_file(data: Path, digest: str, size: int = 160, *, grey: bool = False) -> Path:
    name = f"{digest}-{size}-grey.webp" if grey else f"{digest}-{size}.webp"
    return data / "thumbnails" / digest[:2] / name


# -- quotas and retention, cache by cache -------------------------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_cache_retention_quotas(engine: Engine, data: Path) -> None:
    """The D17 defaults of an unconfigured instance, and the sweep keeps each cache under its
    quota: what the retention no longer keeps goes, then the least recently used, until the
    quota holds."""
    assert CachePolicy.from_settings(Settings()) == policy()

    # thumbnails: A is past its retention, B is older than C, and the quota fits C alone
    digest_a = add_thumbnail(engine, data, "A", used_age_days=400)
    digest_b = add_thumbnail(engine, data, "B", used_age_days=10)
    digest_c = add_thumbnail(engine, data, "C", used_age_days=1)
    _write(data, f"thumbnails/{digest_c[:2]}/{digest_c}-160-grey.webp", b"g" * 32, age_days=1)

    # renditions (KEPUB is a kind of rendition): R is past its retention, B is older than C
    add_rendition(engine, data, "R", age_days=200)
    add_rendition(engine, data, "B", age_days=10)
    rendition_c = add_rendition(engine, data, "C", age_days=1)

    # the HTTP cache: H is expired, B is older than C
    add_http(engine, data, "h-old", age_days=40, max_age=3600)
    add_http(engine, data, "h-b", age_days=10, max_age=400 * DAY)
    add_http(engine, data, "h-c", age_days=1, max_age=400 * DAY)

    # the metadata cache: M is expired; the row quota keeps the two most recently fetched
    add_metadata(engine, "m-expired", fetched_age_days=30, expires_age_days=1)
    add_metadata(engine, "m-old", fetched_age_days=10, expires_age_days=-400)
    add_metadata(engine, "m-mid", fetched_age_days=5, expires_age_days=-400)
    add_metadata(engine, "m-new", fetched_age_days=1, expires_age_days=-400)

    sweep = purge_caches(
        engine, data, ManualClock(NOW),
        policy(thumbnails_quota_bytes=1500, renditions_quota_bytes=1500, http_quota_bytes=1000,
               metadata_rows=2),
    )  # fmt: skip

    # thumbnails: the retention took A, the quota took B; C and its grey variant stay
    assert not _thumbnail_file(data, digest_a).exists()
    assert not _thumbnail_file(data, digest_b).exists()
    assert _thumbnail_file(data, digest_c).exists()
    assert _thumbnail_file(data, digest_c, grey=True).exists()
    assert _rows(engine, storage.thumbnail) == 1
    assert sweep.thumbnails_files == 2 and sweep.thumbnails_rows == 2

    # renditions: the retention took R, the quota took B; C stays
    for seed in ("R", "B"):
        out = _hex(seed)
        assert not (data / f"cache/renditions/{out[:2]}/{out}.jpeg").exists()
    assert (data / rendition_c.rel).exists()
    assert _rows(engine, storage.rendition) == 1
    assert sweep.renditions_files == 2 and sweep.renditions_rows == 2

    # the HTTP cache: the expiry took the old row, the quota took B; C stays
    assert not (data / "cache/http/h-old").exists()
    assert not (data / "cache/http/h-b").exists()
    assert (data / "cache/http/h-c").exists()
    assert _rows(engine, platform.http_cache) == 1
    assert sweep.http_files == 2 and sweep.http_rows == 2

    # the metadata cache: the expired row went, the row quota kept the two most recent
    assert _rows(engine, platform.metadata_cache) == 2
    with engine.connect() as connection:
        kept = set(connection.execute(sa.select(platform.metadata_cache.c.key_hash)).scalars())
    assert kept == {_hex("m-mid"), _hex("m-new")}
    assert sweep.metadata_rows == 2

    # after the sweep, no cache is over its quota
    with engine.connect() as connection:
        state = {entry["kind"]: entry for entry in cache_state(connection, policy())}
    assert state["thumbnails"]["over"] is False
    assert state["renditions"]["over"] is False


# -- host-created paths only --------------------------------------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_cache_eviction_only_on_host_created_paths(engine: Engine, data: Path) -> None:
    """The sweep erases only what a row names, at a name the host writes; anything else in the
    cache folders is not its business, and `erase_cache_path` refuses a path that is not one of
    the cache paths (an original, a projection, a climb)."""
    foreign_thumb = _write(data, "thumbnails/aa/foreign.txt", b"keep me")
    foreign_rendition = _write(data, "cache/renditions/aa/not-a-name.jpeg", b"keep me")
    orphan = _write(data, f"thumbnails/{_hex('orphan')[:2]}/{_hex('orphan')}-160.webp", b"no row")
    # a row whose file is a link: the link goes, never what it points at
    target = _write(data, "outside/target.txt", b"an original elsewhere")
    linked = add_rendition(engine, data, "linked", age_days=100)
    os.remove(data / linked.rel)
    os.symlink(target, data / linked.rel)

    sweep = purge_caches(
        engine,
        data,
        ManualClock(NOW),
        policy(renditions_quota_bytes=1, renditions_retention_days=1),
    )

    assert foreign_thumb.exists() and foreign_rendition.exists()
    assert orphan.exists()  # a file no row names is the orphan sweep's business, not the quota's
    assert target.exists()  # a link is removed as a link, its target is never followed
    assert not (data / linked.rel).exists() and sweep.renditions_files == 1

    # the one eraser of the sweep refuses everything that is not a cache path
    cover = _write(data, f"covers/{_hex('cover')}.webp", b"the original of a thumbnail")
    projection = _write(data, f"meta/units/{_hex('unit')[:2]}/{_hex('unit')}.json", b"{}")
    for refused in (
        f"covers/{_hex('cover')}.webp",
        f"meta/units/{_hex('unit')[:2]}/{_hex('unit')}.json",
        "config.toml",
        "store/works/W/content.cbz",
        "../store/works/W/content.cbz",
        "/store/works/W/content.cbz",
        f"thumbnails/{_hex('climb')[:2]}/../../escape.webp",
        "",
    ):
        with pytest.raises(ValueError):
            erase_cache_path(data, refused)
    assert cover.exists() and projection.exists()


# -- the emitter of the purge is a system emitter of the closed list ----------------------------


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_cache_purge_is_a_listed_system_emitter() -> None:
    """The purge of the derived caches is a system emitter (passe 13, N2): its site is classed
    under its name, its name is in the closed list, and fsops — which has no root under /data —
    refuses every path for it: the perimeter is the cache paths allowlist of the sweep itself."""
    assert CACHE_PURGE_EMITTER in SYSTEM_EMITTERS
    assert (
        DELETION_SITES["shelfhold/library/application/cache_quotas.py::purge_caches"]
        == CACHE_PURGE_EMITTER
    )
    assert CACHE_PURGE_EMITTER in EMITTER_PERIMETERS
    perimeter = EMITTER_PERIMETERS[CACHE_PURGE_EMITTER]
    for rel in (
        "manga/Series/Dr Stone - Ch.1.cbz",
        ".shelfhold/staging/uuid/part",
        ".shelfhold/recycle/01JC/chapter.cbz",
        ".shelfhold/lease",
        ".shelfhold-mount-id",
        f"cache/renditions/{_hex('x')[:2]}/{_hex('x')}.jpeg",
        "",
    ):
        assert not perimeter.allows(rel), f"{CACHE_PURGE_EMITTER} reaches {rel!r}"
    with pytest.raises(EmitterPerimeterError):
        require_inside(CACHE_PURGE_EMITTER, "manga/Series/Dr Stone - Ch.1.cbz")


# -- never /store, /library, .shelfhold/, nor an original ---------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_cache_eviction_never_touches_store_or_originals(
    engine: Engine, data: Path, tmp_path: Path
) -> None:
    """An aggressive sweep (every quota at its smallest, every retention at its shortest) leaves
    the kept copies, the library, the reserved folders and the originals alone: the caches are
    under /data, and only the cache paths of the allowlist are ever erased."""
    store = tmp_path / "store"
    library = tmp_path / "library"
    kept = _write(store, "works/01W/editions/01E/units/001-01U/content.cbz", b"the kept copy")
    lease = _write(library, "manga/Dr Stone/.shelfhold/lease", b"the instance lease")
    exported = _write(library, "manga/Dr Stone/Dr Stone - Ch.1.cbz", b"an exported file")
    cover = _write(data, f"covers/{_hex('cover')}.webp", b"the original of a thumbnail")
    unit_json = _write(data, f"meta/units/{_hex('unit')[:2]}/{_hex('unit')}.json", b"{}")
    add_thumbnail(engine, data, "T", used_age_days=400)
    add_rendition(engine, data, "K", age_days=200, kind="kepub")

    purge_caches(
        engine, data, ManualClock(NOW),
        policy(thumbnails_quota_bytes=1, thumbnails_retention_days=1,
               renditions_quota_bytes=1, renditions_retention_days=1),
    )  # fmt: skip

    assert kept.exists() and lease.exists() and exported.exists()
    assert cover.exists() and unit_json.exists()
    assert _rows(engine, storage.thumbnail) == 0 and _rows(engine, storage.rendition) == 0


# -- a rendition served by an in-flight ticket is pinned ----------------------------------------


@pytest.mark.req("EF-BIB-018")
def test_a_rendition_served_by_an_inflight_ticket_is_not_evicted(
    engine: Engine, data: Path
) -> None:
    """While a request reads a rendition, the sweep cannot evict it: the read pins the file for
    the time of the ticket. Once the ticket is done, the next sweep takes it like any other."""
    service = RenditionService(engine, data, ManualClock(NOW), tombstones=NONE_PURGED)
    older = add_rendition(engine, data, "older", age_days=10)
    served = add_rendition(engine, data, "served", age_days=1)

    with service.serving(served):  # the ticket is being served: the rendition is pinned
        assert RenditionService.pinned() == {served.content_hash}
        purge_caches(
            engine,
            data,
            ManualClock(NOW),
            policy(renditions_quota_bytes=1500),
            pinned=RenditionService.pinned(),
        )
        assert not (data / older.rel).exists()  # the least recently used goes
        assert (data / served.rel).exists()  # the pinned one stays
    assert RenditionService.pinned() == frozenset()

    purge_caches(engine, data, ManualClock(NOW), policy(renditions_quota_bytes=500))
    assert not (data / served.rel).exists()  # no ticket any more: the quota takes it


@pytest.mark.req("EF-BIB-018")
def test_rendition_access_updates_the_lru_key(engine: Engine, data: Path) -> None:
    """Reading a rendition records the access on the file (at most once a day): the LRU key of
    the sweep is the last access, not the creation."""
    body = b"the converted bytes"
    digest = sha256(body).hexdigest()
    _write(data, f"cache/renditions/{digest[:2]}/{digest}.jpeg", body, age_days=30)
    source = f"sha256:{_hex('lru-src')}"
    with engine.begin() as connection:
        connection.execute(
            sa.insert(storage.rendition).values(
                source_content_hash=source,
                kind="jpeg",
                params={"src": digest},
                content_hash=f"sha256:{digest}",
                size=len(body),
                created_at=_age(30),
            )
        )
    service = RenditionService(engine, data, ManualClock(NOW), tombstones=NONE_PURGED)
    rendition = Rendition(source, "jpeg", f'{{"src": "{digest}"}}', f"sha256:{digest}", len(body))

    assert service.read(rendition) == body
    touched = os.stat(data / rendition.rel).st_mtime
    # the access was recorded at the clock of the service, not the creation of a month ago
    assert touched == pytest.approx(NOW.timestamp(), abs=2)

    recent = NOW.timestamp() - 3600  # an hour ago: within the once-a-day grace, not touched again
    os.utime(data / rendition.rel, (recent, recent))
    assert service.read(rendition) == body
    assert os.stat(data / rendition.rel).st_mtime == pytest.approx(recent, abs=2)  # once a day


# -- the state of the caches in `shelfhold status` and the Health page --------------------------


@pytest.mark.req("EF-BIB-018")
def test_cache_state_in_status_and_health(engine: Engine, data: Path) -> None:
    """Every cache shows its use, its quota and whether it is over, in `shelfhold status` and on
    the Health page: read from the database alone, no disk is opened."""
    add_thumbnail(engine, data, "T", used_age_days=1, bytes_=2048)
    add_rendition(engine, data, "K", age_days=1, size=4096)
    add_http(engine, data, "h", age_days=1, max_age=DAY)
    add_metadata(engine, "m", fetched_age_days=1, expires_age_days=1)
    add_metadata(engine, "m2", fetched_age_days=2, expires_age_days=-1)  # two rows, a quota of one

    limits = policy(thumbnails_quota_bytes=1024, renditions_quota_bytes=1024, metadata_rows=1)
    with engine.connect() as connection:
        state = {entry["kind"]: entry for entry in cache_state(connection, limits)}
    assert state["thumbnails"] == {
        "kind": "thumbnails", "entries": 1, "used_bytes": 2048, "quota_bytes": 1024, "over": True
    }  # fmt: skip
    assert state["renditions"]["used_bytes"] == 4096 and state["renditions"]["over"] is True
    assert state["http"]["entries"] == 1
    metadata = state["metadata"]
    assert metadata["kind"] == "metadata" and metadata["entries"] == 2
    assert metadata["quota_entries"] == 1  # the inner bound, in rows
    assert metadata["quota_bytes"] == 256 * 1024 * 1024  # the D17 bound, in bytes of payloads
    assert isinstance(metadata["used_bytes"], int) and metadata["used_bytes"] > 0
    assert metadata["over"] is True  # two rows, a quota of one

    services = OpsServices(
        engine, clock=ManualClock(NOW), bus=EventBus(ManualClock(NOW)), cache_policy=limits
    )
    assert services.status().as_dict()["caches"] == [
        state["thumbnails"],
        state["renditions"],
        state["http"],
        state["metadata"],
    ]

    settings = Settings(data_dir=data)
    page = build_health_page(engine, settings, NOW)
    assert [entry["kind"] for entry in page["caches"]] == [
        "thumbnails", "renditions", "http", "metadata"
    ]  # fmt: skip
    with engine.connect() as connection:  # the page shows the state under the settings' own policy
        assert page["caches"] == cache_state(connection, CachePolicy.from_settings(settings))


@pytest.mark.req("EF-BIB-018")
def test_the_d17_defaults_of_an_unconfigured_instance() -> None:
    """The D17 bounds of an instance nobody configured: two GiB of thumbnails, five of renditions
    (a KEPUB is one), 512 MiB of HTTP bodies, 256 MiB of metadata payloads, and ninety days
    without an access everywhere."""
    policy = CachePolicy.from_settings(Settings(data_dir=Path("/data")))
    assert policy.thumbnails_quota_bytes == 2 * 1024**3
    assert policy.renditions_quota_bytes == 5 * 1024**3
    assert policy.http_quota_bytes == 512 * 1024**2
    assert policy.metadata_quota_bytes == 256 * 1024**2
    assert (
        policy.thumbnails_retention_days,
        policy.renditions_retention_days,
        policy.http_retention_days,
    ) == (90, 90, 90)


@pytest.mark.req("EF-BIB-018")
def test_the_low_disk_guard_passes_before_the_quotas(engine: Engine, data: Path) -> None:
    """While the guard of M3 says the volume of /data is under its free-space mark, the sweep
    evicts what the quotas would have kept, until the volume is back above (D17)."""
    rendition = add_rendition(engine, data, "K", age_days=1, size=100)
    purge_caches(engine, data, ManualClock(NOW), policy(renditions_quota_bytes=4096))
    assert (data / rendition.rel).exists()  # under its quota: the sweep leaves it

    purge_caches(
        engine, data, ManualClock(NOW), policy(renditions_quota_bytes=4096), pressure=lambda: True
    )
    assert not (data / rendition.rel).exists()  # the disk is low: the guard passes first
