"""Review of #217 (M3 L24), adversary's proofs of concept. Each test is red on the PR's head.

- the lazy generation holds a request thread for as long as the decode worker runs, with no
  single flight and no bound on the wait: a library screen of uncached covers stalls every
  synchronous route of the API (here `/healthz`);
- the thumbnail folder's shard is followed when it is a link: the collection deletes, and the
  lazy generation writes, outside `/data`;
- a file found at a thumbnail's name with no row is served as the thumbnail and recorded,
  unverified, for an immutable day in every browser.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import cover_ref_visible
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure import imaging
from shelfhold.infrastructure.schema import storage
from shelfhold.library.application.thumbnails import Pending, ThumbnailService
from tests.integration import test_thumbnails as service_tests
from tests.integration import test_thumbnails_http as http_tests
from tests.integration.test_thumbnails import Rig
from tests.integration.test_thumbnails_http import World, url

rig = service_tests.rig  # the fixtures of the PR's own tests, reused as they are
world = http_tests.world


MAX_HELD = 8  # threads the decodes may hold of the server's 40: the rest answer at once


def _slow_worker(monkeypatch: pytest.MonkeyPatch, delay: float) -> list[int]:
    """The real worker, made as slow as a cold start on a small NAS; counts its runs."""
    real, calls = imaging.make_thumbnail, []
    lock = threading.Lock()

    def slow(*args: Any, **kwargs: Any) -> Any:
        with lock:
            calls.append(1)
        time.sleep(delay)
        return real(*args, **kwargs)

    monkeypatch.setattr("shelfhold.library.application.thumbnails.make_thumbnail", slow)
    return calls


def _flood(client: Any, paths: list[str]) -> tuple[list[threading.Thread], list[int]]:
    codes: list[int] = []

    def one(path: str) -> None:
        codes.append(client.get(path).status_code)

    threads = [threading.Thread(target=one, args=(p,), daemon=True) for p in paths]
    for thread in threads:
        thread.start()
    return threads, codes


@pytest.mark.req("EF-BIB-020", "ENF-SEC-003")
def test_review_lazy_thumbnail_is_made_once_under_concurrent_requests(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight tabs (or one screen that asks twice) for the same uncached thumbnail: the worker runs
    eight times. Nothing coalesces the requests for one (hash, size)."""
    calls = _slow_worker(monkeypatch, 0.4)
    threads, codes = _flood(world.admin, [url(world.digests["ok"], 320)] * 8)
    for thread in threads:
        thread.join(30)
    assert codes == [200] * 8
    assert len(calls) == 1, f"the worker ran {len(calls)} times for one thumbnail"


@pytest.mark.req("EF-BIB-020", "ENF-SEC-003")
def test_review_uncached_screen_does_not_stall_the_api(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screen of sixty uncached covers (any profile may ask it, a child included): each request
    waits on `BoundedSemaphore(2)` inside a thread of the server's pool (40), with no timeout.
    The pool is full; a liveness probe, a login, any synchronous route waits behind the decodes."""
    _slow_worker(monkeypatch, 0.5)
    digest = world.digests["ok"]
    threads, _ = _flood(world.admin, [url(digest, (160, 320, 640)[i % 3]) for i in range(60)])
    time.sleep(0.5)
    started = time.perf_counter()
    probe = world.admin.get("/healthz")
    waited = time.perf_counter() - started
    for thread in threads:
        thread.join(60)
    assert probe.status_code == 200
    assert waited < 0.3, f"/healthz waited {waited:.1f} s behind the thumbnail decodes"


@pytest.fixture
def one_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The database pool reduced to a single connection that nobody waits long for: whatever asks
    a second one while it holds the first is caught at once, not after the pool's 30 s."""
    real = sa.create_engine
    monkeypatch.setattr(
        "shelfhold.infrastructure.db.create_engine",
        lambda *args, **kwargs: real(*args, pool_size=1, max_overflow=0, pool_timeout=2, **kwargs),
    )


@pytest.mark.req("EF-BIB-020", "ENF-OPS-003")
def test_review_healthz_never_holds_a_connection_while_asking_for_another(
    one_connection: None, world: World
) -> None:
    """`/healthz` of a session read the queues with one connection held and the readers of the
    waits opening a second one: sixteen probes at once (the pool is 5 + 10) emptied the pool and
    every one of them, with the thumbnails behind them, waited for the pool's timeout."""
    started = time.perf_counter()
    probe = world.admin.get("/healthz")
    assert probe.status_code == 200, probe.text
    assert time.perf_counter() - started < 1.5, "/healthz waited for a connection of its own pool"


@pytest.mark.req("EF-BIB-020", "ENF-SEC-003")
def test_review_uncached_screen_holds_only_a_few_request_threads(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof the timing test cannot give on a slow runner: however many requests ask for an
    uncached cover, only a handful of threads may be held by the decodes (a leader per slot, a few
    waiters); every other request answers at once. The server's pool is 40 threads shared with the
    synchronous routes: the probe never waits behind a queue that the decodes keep refilled."""
    release, entered = threading.Event(), threading.Event()
    real = imaging.make_thumbnail

    def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(30)
        return real(*args, **kwargs)

    monkeypatch.setattr("shelfhold.library.application.thumbnails.make_thumbnail", blocked)
    service = ThumbnailService(
        rig.engine, rig.data, rig.clock, limits=service_tests.LIMITS, wait=30
    )
    digest = rig.cover("ok", (30, 70, 10))
    answers: list[str] = []

    def ask(size: int) -> None:
        try:
            service.get(digest, size)
            answers.append("made")
        except Pending:
            answers.append("pending")

    threads = [
        threading.Thread(target=ask, args=((160, 320, 640)[i % 3],), daemon=True) for i in range(60)
    ]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(10)
        time.sleep(1.0)  # the requests that answer at once are long gone
        held = sum(thread.is_alive() for thread in threads)
        assert held <= MAX_HELD, f"{held} of 60 request threads are held behind the decodes"
        assert answers.count("pending") >= 60 - MAX_HELD
    finally:
        release.set()
        for thread in threads:
            thread.join(30)


def _outside(made: Rig) -> Path:
    outside = made.data.parent / "outside-of-data"
    outside.mkdir(exist_ok=True)
    return outside


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_review_gc_does_not_follow_a_linked_shard_out_of_data(rig: Rig) -> None:
    """`collect` unlinks `self._path(...)` with `Path.unlink`: the last name is not followed, but
    the shard on the way is. The sweep skips a linked shard; the orphan branch does not. The
    house rule for `/data` deletions (`volumes.erase_data_path`) is a dirfd walk, O_NOFOLLOW."""
    digest = "ef" * 32  # an orphan: no Edition points at it
    outside = _outside(rig)
    victim = outside / f"{digest}-160.webp"
    victim.write_bytes(b"keep")
    folder = rig.data / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / digest[:2]).symlink_to(outside, target_is_directory=True)
    with rig.engine.begin() as connection:
        connection.execute(
            sa.insert(storage.thumbnail).values(
                content_hash=f"sha256:{digest}", size=160, path=f"{digest[:2]}/{digest}-160.webp",
                bytes=4, created_at=rig.clock.now(),
            )
        )  # fmt: skip
    rig.service.collect(set(), closing=lambda: False)
    assert victim.exists(), "the collection deleted a file outside /data through a linked shard"


@pytest.mark.req("EF-BIB-020", "ENF-SEC-010")
def test_review_lazy_generation_does_not_write_through_a_linked_shard(rig: Rig) -> None:
    """`_make` does `mkdir(parents=True, exist_ok=True)` then `write_private` on the joined path:
    O_NOFOLLOW covers the temporary's last name only. A linked shard sends the write (and later
    the read, `_read_regular`) outside `/data`."""
    digest = rig.cover("ok", (70, 10, 10))
    outside = _outside(rig)
    folder = rig.data / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / digest[:2]).symlink_to(outside, target_is_directory=True)
    rig.service.get(digest, 160)
    assert list(outside.iterdir()) == [], "a thumbnail was written outside /data"


@pytest.mark.req("EF-BIB-020")
def test_review_file_without_row_is_not_served_unverified(rig: Rig) -> None:
    """A file at the computed name with no row (a crash between the link and the insert, a
    restore, a bug) is not re-made: `_make` decodes, hits `FileExistsError`, serves the bytes on
    disk as they are and records them. The route then sends them as `image/webp`, immutable."""
    digest = rig.cover("ok", (10, 70, 10))
    path = rig.data / "thumbnails" / digest[:2] / f"{digest}-160.webp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<svg onload=alert(1)>not a webp</svg>")
    made = rig.service.get(digest, 160)
    assert made is None or (made.data[:4] == b"RIFF" and made.data[8:12] == b"WEBP"), made


@pytest.mark.req("EF-BIB-020", "ENF-PERF-010")
def test_review_cover_filter_is_not_a_scan_of_every_edition_field(world: World) -> None:
    """Every thumbnail request runs `cover_ref_visible`, which filters `metadata_field` on
    (entity_type, field, value): no index has `value`, so each of the sixty requests of a screen
    reads every field of every Edition. The plan must reach the row by an index on the value or
    by the Edition."""
    statements: list[tuple[str, Any]] = []
    engine = world.seed.engine

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *rest: Any) -> None:
        if "metadata_field" in statement:
            statements.append((statement, params))

    sa.event.listen(engine, "before_cursor_execute", capture)
    try:
        with engine.connect() as connection:
            kid = connection.execute(
                sa.text("SELECT id FROM user WHERE username='kid'")
            ).scalar_one()
            for user in (kid, None):
                scope = reader_scope(connection, user) if user else None
                if scope is None:
                    admin = connection.execute(
                        sa.text("SELECT id FROM user WHERE role='admin' LIMIT 1")
                    ).scalar_one()
                    scope = reader_scope(connection, admin)
                cover_ref_visible(connection, scope, f"covers/{world.digests['ok']}.webp")
    finally:
        sa.event.remove(engine, "before_cursor_execute", capture)
    assert statements
    with engine.connect() as connection:
        for statement, params in statements:
            plan = [
                str(row[-1])
                for row in connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", params)
            ]
            field_steps = [step for step in plan if "metadata_field" in step]
            assert field_steps and all(
                "value=" in step or "entity_id=" in step for step in field_steps
            ), plan
