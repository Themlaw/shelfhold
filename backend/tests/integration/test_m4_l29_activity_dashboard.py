"""M4 L29 (EF-INT-019, EF-INT-020, ENF-SEC-010): the activity history and the blocklist, the storage
and network dashboard.

The history and the dashboard are the administrator's alone (a session, or a key of scope `admin`);
a child, a member and a read-only key are refused before anything is read. A chapter on the
blocklist is ignored when its source offers it again, and only the administrator takes it off,
by name, with an audit line. The traffic counter holds a source, a day and two numbers: never an
address. Real application, real sessions, real keys; synthetic names only.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.blocklist import (
    BLOCK_REASONS,
    block_unit,
    blocked_ids,
    list_blocks,
    unblock,
)
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.fetcher.client import GuardedFetcher
from shelfhold.infrastructure.fetcher.errors import SsrfBlocked
from shelfhold.infrastructure.fetcher.traffic_store import SqlTrafficCounter
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.sdk.refs import SeriesRef
from tests.auth_env import CSRF_COOKIE, CSRF_HEADER, AuthEnv, db_rows
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.follow_env import Bench, follow_bench
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.visibility_env import add_profile

PREFIX = "/api/v1/ops"
HISTORY = f"{PREFIX}/activity-history"
BLOCKLIST = f"{PREFIX}/blocklist"
STORAGE = f"{PREFIX}/storage-dashboard"
SOURCE_KEY = "suwayomi:7001"  # the source of the bench's series
OTHER_KEY = "other-source:1"
PATH_CANARY = "/srv/synthetic-library/secret-shelf/ch-001.cbz"
HOST_CANARY = "canary-host.invalid"
IP_CANARY = "10.77.88.99"
SECRET_CANARY = "sk-" + new_ulid().lower()  # a marker built at run time, not a secret
CANARIES = (PATH_CANARY, HOST_CANARY, IP_CANARY, SECRET_CANARY, "synthetic-library")


# -- the cycle: a blocked chapter is ignored when its source offers it again ---------------------


def _third_chapter(bench: Bench) -> str:
    """The `remote_unit_id` the source gives to the chapter that has just been added."""
    bench.add_chapter(3)
    ref = SeriesRef(source_key=SOURCE_KEY, remote_id="/series-alpha", title="")
    units = asyncio.run(bench.service.list_units(bench.fetcher, ref))
    return next(u.remote_unit_id for u in units if u.number == "3")


def _variants(bench: Bench) -> set[str]:
    return {r["remote_unit_id"] for r in bench.rows("SELECT remote_unit_id FROM unit_variant")}


def _block(bench: Bench, remote_unit_id: str, source_key: str = SOURCE_KEY) -> str:
    with write_transaction(bench.lib.engine) as connection:
        return block_unit(
            connection, source_key=source_key, remote_unit_id=remote_unit_id,
            reason="bad_quality", actor_id=None, now=datetime.now(UTC),
        ).id  # fmt: skip


@pytest.mark.req("EF-INT-019")
def test_activity_history_and_blocklist() -> None:
    """Given a blocked chapter, when the source offers it again, then it is ignored."""
    with follow_bench() as bench:
        third = _third_chapter(bench)
        _block(bench, third)
        report = bench.run()
        assert third not in _variants(bench)  # no Unit, no request, no file
        assert report.blocked == 1
        assert len(_variants(bench)) >= 2  # the others of the same listing came in
        again = bench.run()  # offered again, ignored again
        assert again.blocked == 1
        assert third not in _variants(bench)
        with bench.lib.engine.connect() as connection:
            (row,) = list_blocks(connection)
        assert row.proposed_count == 2  # the history knows how often it was offered
        assert row.last_proposed_at is not None


@pytest.mark.req("EF-INT-019")
def test_blocklist_never_unblocks_silently() -> None:
    """Nothing but the administrator's explicit removal lifts a block: not time, not a new cycle,
    not a series that is unbound and bound again, not the account that blocked it being gone."""
    with follow_bench() as bench:
        third = _third_chapter(bench)
        block_id = _block(bench, third)
        for _ in range(3):
            bench.run()
        bench.clock.advance(days=400)  # a year later, still blocked
        bench.run()
        assert third not in _variants(bench)
        with bench.lib.engine.connect() as connection:
            assert blocked_ids(connection, SOURCE_KEY) == frozenset({third})
            assert blocked_ids(connection, OTHER_KEY) == frozenset()  # by source: not elsewhere
        with write_transaction(bench.lib.engine) as connection:
            assert unblock(connection, block_id) is True  # the one way out
            assert unblock(connection, block_id) is False  # and it says so when nothing was there
        bench.run()
        assert third in _variants(bench)  # offered again, taken like any chapter


@pytest.mark.req("EF-INT-019", "ENF-SEC-005")
def test_blocklist_reasons_are_a_closed_list() -> None:
    assert set(BLOCK_REASONS) == {"bad_quality", "wrong_content", "duplicate", "other"}
    with (
        follow_bench() as bench,
        pytest.raises(ValueError, match="reason"),
        write_transaction(bench.lib.engine) as connection,
    ):
        block_unit(
            connection, source_key=SOURCE_KEY, remote_unit_id="x",
            reason="a free text with a path /srv/x", actor_id=None, now=datetime.now(UTC),
        )  # fmt: skip


# -- the routes: the administrator's, and nobody else's -----------------------------------------


def _key(world: World, username: str, **kwargs: Any) -> str:
    engine = create_db_engine(world.env.settings.database_path)
    try:
        return (
            local_auth(engine, world.env.settings)
            .create_api_key(name="k", username=username, **kwargs)
            .value
        )
    finally:
        engine.dispose()


def _bearer(world: World, key: str, ip: str) -> Any:
    client = world.env.other_client(ip)
    client.headers["Authorization"] = f"Bearer {key}"
    return client


def _write(world: World, client: Any, method: str, path: str, body: Any = None) -> Any:
    # a session sends the CSRF token it holds (an attacker without one sends a made-up one)
    headers = (
        {}
        if "Authorization" in client.headers
        else {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or "made-up"}
    )
    return client.request(method, path, json=body, headers=headers)


def _readers(world: World) -> dict[str, Any]:
    """Every reader that is not the administrator's session, or an `admin` key."""
    member = add_profile(world.env, "ana", "member", "10.0.0.3")
    return {
        "child": world.kid,
        "member": member,
        "child_key": _bearer(world, _key(world, "kid", read_only=True), "10.0.0.20"),
        "member_key": _bearer(world, _key(world, "ana", read_only=True), "10.0.0.21"),
        "admin_read_key": _bearer(world, _key(world, "admin", read_only=True), "10.0.0.22"),
        "nobody": world.env.other_client("10.0.0.23"),
    }


def _insert_block(world: World, remote_unit_id: str = "ch-keep") -> str:
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with write_transaction(engine) as connection:
            return block_unit(
                connection, source_key=SOURCE_KEY, remote_unit_id=remote_unit_id,
                reason="duplicate", actor_id=None, now=datetime.now(UTC),
            ).id  # fmt: skip
    finally:
        engine.dispose()


@pytest.mark.req("EF-INT-019", "ENF-SEC-010")
def test_blocklist_admin_roundtrip_audited(world: World) -> None:
    admin = world.env.client
    body = {"source_key": SOURCE_KEY, "remote_unit_id": "ch-009", "reason": "bad_quality"}
    made = _write(world, admin, "POST", BLOCKLIST, body)
    assert made.status_code == 201, made.text
    block_id = made.json()["id"]
    assert set(made.json()) >= {"id", "source_key", "ref", "reason", "created_at"}
    assert (
        "ch-009" not in made.text
    )  # the source's identifier is never echoed (it can be an address)
    again = _write(world, admin, "POST", BLOCKLIST, body)  # the same chapter: the same block
    assert again.status_code == 200 and again.json()["id"] == block_id
    bad = _write(world, admin, "POST", BLOCKLIST, {**body, "reason": "free text /srv/x"})
    assert bad.status_code in (400, 422)
    listed = admin.get(BLOCKLIST)
    assert [b["id"] for b in listed.json()["items"]] == [block_id]
    assert listed.json()["items"][0]["proposed_count"] == 0

    gone = _write(world, admin, "DELETE", f"{BLOCKLIST}/{block_id}")
    assert gone.status_code == 204
    assert admin.get(BLOCKLIST).json()["items"] == []
    # the block and its removal are named in the audit, with who and when (never silent)
    audited = db_rows(
        world.env.settings,
        "SELECT action, target, actor_id FROM audit "
        "WHERE action LIKE 'blocklist.%' ORDER BY at, action",
    )
    assert [row[0] for row in audited] == ["blocklist.add", "blocklist.remove"]
    assert all(block_id in str(row[1]) and row[2] for row in audited)  # which block, and who
    missing = _write(world, admin, "DELETE", f"{BLOCKLIST}/{new_ulid()}")
    assert missing.status_code == 404


@pytest.mark.req("EF-INT-019")
def test_a_chapter_is_blocked_by_its_unit_and_shown_by_title_and_number(world: World) -> None:
    """The administrator names the chapter by our own Unit and the source that offered it: the
    source's identifier is resolved here, never echoed, and the screen reads title and number."""
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with write_transaction(engine) as connection:
            work = add_work(connection)
            edition = add_edition(connection, work)
            item = add_source_item(connection)
            binding = add_binding(connection, work, edition, item)
            unit = add_unit(connection, edition, "7")
            add_variant(connection, unit, binding, "https://upstream.invalid/ch-7")
    finally:
        engine.dispose()
    admin = world.env.client
    body = {"source_key": "suwayomi:1", "unit_id": unit, "reason": "wrong_content"}
    made = _write(world, admin, "POST", BLOCKLIST, body)
    assert made.status_code == 201, made.text
    assert made.json()["number"] == "7" and made.json()["title"]
    assert "upstream.invalid" not in made.text  # what the source calls it is not an address we show
    assert admin.get(BLOCKLIST).json()["items"][0]["title"] == made.json()["title"]
    # a source that never offered that Unit: nothing to block; naming it twice, or not at all: 422
    other = _write(world, admin, "POST", BLOCKLIST, {**body, "source_key": "elsewhere:1"})
    assert other.status_code == 404
    for bad in ({**body, "remote_unit_id": "x"}, {"source_key": "s", "reason": "other"}):
        assert _write(world, admin, "POST", BLOCKLIST, bad).status_code in (400, 422)


@pytest.mark.req("EF-INT-019", "ENF-SEC-010")
def test_blocklist_attacks_child_member_key_idor(world: World) -> None:
    """A child, a member, a read-only key (the administrator's own included) and nobody: refused
    on the three routes, with one answer whether the block exists or not (IDOR), nothing moved."""
    block_id = _insert_block(world)
    readers = _readers(world)
    for name, client in readers.items():
        listing = client.get(BLOCKLIST)
        assert listing.status_code in (401, 403), name
        assert "ch-keep" not in listing.text and block_id not in listing.text, name
        post = _write(
            world, client, "POST", BLOCKLIST,
            {"source_key": SOURCE_KEY, "remote_unit_id": "ch-evil", "reason": "other"},
        )  # fmt: skip
        assert post.status_code in (401, 403), name
        mine = _write(world, client, "DELETE", f"{BLOCKLIST}/{block_id}")  # a block that exists
        none = _write(world, client, "DELETE", f"{BLOCKLIST}/{new_ulid()}")  # one that does not
        assert mine.status_code in (401, 403), name
        assert (mine.status_code, mine.json().get("type")) == (
            none.status_code, none.json().get("type"),
        ), f"{name}: the answer tells whether the block exists"  # fmt: skip
    engine = create_db_engine(world.env.settings.database_path)
    try:
        with engine.connect() as connection:
            assert [b.id for b in list_blocks(connection)] == [
                block_id
            ]  # nothing added, nothing lifted
    finally:
        engine.dispose()
    assert db_rows(world.env.settings, "SELECT 1 FROM audit WHERE action LIKE 'blocklist.%'") == []
    # a key of scope `admin` is the administrator's: it reads and writes
    admin_key = _bearer(world, _key(world, "admin"), "10.0.0.24")
    assert admin_key.get(BLOCKLIST).status_code == 200
    assert admin_key.delete(f"{BLOCKLIST}/{block_id}").status_code == 204


# -- the history -------------------------------------------------------------------------------


def _seed_events(world: World) -> list[int]:
    """Operating events of the household, one of them carrying what must never be shown."""
    engine = create_db_engine(world.env.settings.database_path)
    ids: list[int] = []
    try:
        with write_transaction(engine) as connection:
            for index, payload in enumerate(
                (
                    {"count": 1},
                    {"count": 2, "path": PATH_CANARY, "host": HOST_CANARY, "ip": IP_CANARY,
                     "token": SECRET_CANARY, "detail": f"{HOST_CANARY} {PATH_CANARY}"},
                    {"count": 3, "series_id": new_ulid()},
                )
            ):  # fmt: skip
                result = connection.execute(
                    sa.insert(platform.event).values(
                        type="series.tombstoned_upstream", schema_version=1, payload=payload,
                        occurred_at=datetime.now(UTC) - timedelta(minutes=10 - index),
                        actor_id=None, visibility="ops",
                    )
                )  # fmt: skip
                ids.append(int(result.inserted_primary_key[0]))
    finally:
        engine.dispose()
    return ids


@pytest.mark.req("EF-INT-019")
def test_activity_history_admin_reads_every_event_without_paths_or_secrets(world: World) -> None:
    ids = _seed_events(world)
    admin = world.env.client
    seen = admin.get(HISTORY, params={"limit": 50})
    assert seen.status_code == 200, seen.text
    items = seen.json()["items"]
    assert {i["event_id"] for i in items} >= set(ids)  # the admin reads the household's events
    assert [i["occurred_at"] for i in items] == sorted(
        (i["occurred_at"] for i in items), reverse=True
    )
    for canary in CANARIES:
        assert canary not in seen.text, f"the history shows {canary}"
    # paged: the cursor gives the older ones, nothing twice
    first = admin.get(HISTORY, params={"limit": 1}).json()
    assert len(first["items"]) == 1 and first["next_before"] is not None
    second = admin.get(HISTORY, params={"limit": 1, "before": first["next_before"]}).json()
    assert second["items"][0]["event_id"] != first["items"][0]["event_id"]
    assert admin.get(HISTORY, params={"limit": 0}).status_code in (400, 422)


@pytest.mark.req("EF-INT-019", "EF-USR-016", "ENF-SEC-010")
def test_activity_history_goes_through_activity_view_for_child_and_member(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one filter of the activity page (`project_event`, `activity_view`) is the history's too:
    every row goes through it, and a child or a member is refused before any row is read, while
    the page they do have shows nothing of the others' events."""
    import shelfhold.ops.activity_history as history

    ids = _seed_events(world)
    passed: list[Any] = []
    real = history.project_event

    def spy(connection: Any, viewer: Any, scope: Any, entry: Any, **kwargs: Any) -> Any:
        passed.append(viewer)
        return real(connection, viewer, scope, entry, **kwargs)

    monkeypatch.setattr(history, "project_event", spy)
    assert world.env.client.get(HISTORY).status_code == 200
    assert len(passed) >= len(ids)  # every row of the history went through the projection
    assert {v.role for v in passed} == {"admin"}
    passed.clear()
    for name, client in _readers(world).items():
        refused = client.get(HISTORY)
        assert refused.status_code in (401, 403), name
        assert "event_id" not in refused.text and "items" not in refused.text, name
    assert passed == []  # refused before the projection: no row was read for them
    # what a child and a member have is the activity page, which filters the same way
    for client in (world.kid, add_profile(world.env, "bob", "member", "10.0.0.4")):
        page = client.get("/api/v1/activity")
        assert page.status_code == 200
        assert not {e["event_id"] for e in page.json()} & set(ids)  # none of the admin's `ops`
        for canary in CANARIES:
            assert canary not in page.text


# -- the dashboard -----------------------------------------------------------------------------


def _seed_storage(world: World) -> dict[str, str]:
    engine = create_db_engine(world.env.settings.database_path)
    day = world.env.clock.now().date()  # the application's own today
    try:
        with write_transaction(engine) as connection:
            root = new_ulid()  # a library root: its path is something the dashboard must not show
            connection.execute(
                sa.insert(platform.physical_root).values(root_id=root, path_entry=PATH_CANARY)
            )
            shelves = {}
            for name in ("Synthetic shelf A", "Synthetic shelf B"):
                shelf_id = new_ulid()
                connection.execute(
                    sa.insert(export.shelf).values(id=shelf_id, name=name, phrase=f"phrase {name}")
                )
                shelves[name] = shelf_id
            for shelf_id, sizes in (
                (shelves["Synthetic shelf A"], ((1, 100, "store"), (2, 50, "library"))),
                (shelves["Synthetic shelf B"], ((3, 7, "store"), (3, 7, "library"))),  # a hard link
            ):
                work = add_work(connection)
                edition = add_edition(connection, work, shelf_id=shelf_id)
                for number, (ino, size, location) in enumerate(sizes, start=1):
                    unit = add_unit(connection, edition, str(number))
                    connection.execute(
                        sa.insert(replica_schema.replica).values(
                            id=new_ulid(), unit_id=unit, location=location,
                            physical_root_id=root if location == "library" else None,
                            rel_path=f"{PATH_CANARY[1:]}-{ino}", dev=1, ino=ino, size=size,
                            independent=True, fidelity="original",
                        )
                    )  # fmt: skip
            loose = add_unit(connection, add_edition(connection, add_work(connection)), "1")
            connection.execute(  # an Edition on no shelf: counted apart, never lost
                sa.insert(replica_schema.replica).values(
                    id=new_ulid(), unit_id=loose, location="store", rel_path="loose/one.cbz",
                    dev=1, ino=77, size=5, independent=True, fidelity="original",
                )
            )  # fmt: skip
            work = add_work(connection)
            edition = add_edition(connection, work, shelf_id=shelves["Synthetic shelf A"])
            unit = add_unit(connection, edition, "9")
            connection.execute(  # damaged: counted for nothing
                sa.insert(replica_schema.replica).values(
                    id=new_ulid(), unit_id=unit, location="store", rel_path="x", dev=1, ino=99,
                    size=1_000_000, independent=True, fidelity="original",
                    damaged_at=datetime.now(UTC),
                )
            )  # fmt: skip
            for source, offset, requests, received in (
                ("source-one", 0, 4, 4000),
                ("source-one", 1, 1, 10),
                ("source-two", 0, 2, 20),
            ):
                connection.execute(
                    sa.text(
                        "INSERT INTO traffic_counter (day, source_key, requests, bytes_in) "
                        "VALUES (:d, :s, :r, :b)"
                    ),
                    {"d": (day - timedelta(days=offset)).isoformat(), "s": source,
                     "r": requests, "b": received},
                )  # fmt: skip
        return shelves
    finally:
        engine.dispose()


@pytest.mark.req("EF-INT-020")
def test_storage_dashboard(world: World) -> None:
    """Given the page, when it is opened, then the space per shelf and the traffic per source."""
    shelves = _seed_storage(world)
    seen = world.env.client.get(STORAGE)
    assert seen.status_code == 200, seen.text
    body = seen.json()
    by_name = {s["name"]: s for s in body["shelves"]}
    a, b = by_name["Synthetic shelf A"], by_name["Synthetic shelf B"]
    assert (a["shelf_id"], a["bytes"], a["copies"]) == (shelves["Synthetic shelf A"], 150, 2)
    assert (a["by_location"]["store"], a["by_location"]["library"]) == (100, 50)
    assert (b["bytes"], b["copies"]) == (7, 1)  # two hard links are one file, a damaged one is none
    (loose,) = [s for s in body["shelves"] if s["shelf_id"] is None]
    assert (loose["name"], loose["bytes"], loose["copies"]) == (None, 5, 1)
    traffic = {s["source_key"]: s for s in body["traffic"]["sources"]}
    assert (traffic["source-one"]["requests"], traffic["source-one"]["bytes_in"]) == (5, 4010)
    assert len(traffic["source-one"]["by_day"]) == 2
    assert traffic["source-two"]["requests"] == 2
    assert body["traffic"]["days"] == 30
    narrow = world.env.client.get(STORAGE, params={"days": 1}).json()
    one = {s["source_key"]: s for s in narrow["traffic"]["sources"]}
    assert one["source-one"]["requests"] == 4  # only today's
    assert world.env.client.get(STORAGE, params={"days": 0}).status_code in (400, 422)
    for canary in CANARIES:  # no path, no host, no address, no secret
        assert canary not in seen.text, f"the dashboard shows {canary}"
    assert "rel_path" not in seen.text


@pytest.mark.req("EF-INT-020", "ENF-SEC-010")
def test_storage_dashboard_admin_only(world: World) -> None:
    _seed_storage(world)
    for name, client in _readers(world).items():
        refused = client.get(STORAGE)
        assert refused.status_code in (401, 403), name
        assert "Synthetic shelf" not in refused.text and "source-one" not in refused.text, name
    admin_key = _bearer(world, _key(world, "admin"), "10.0.0.25")
    assert admin_key.get(STORAGE).status_code == 200  # the `admin` scope is the administrator's


# -- the counter: after the guard, a source and a day, never an address -------------------------


def _counter_rows(api: AuthEnv) -> list[tuple[Any, ...]]:
    return db_rows(api.settings, "SELECT * FROM traffic_counter")


@pytest.mark.req("EF-INT-020", "ENF-SEC-013")
def test_traffic_counter_has_no_url(api: AuthEnv) -> None:
    columns = {r[1] for r in db_rows(api.settings, "PRAGMA table_info(traffic_counter)")}
    assert columns == {"day", "source_key", "requests", "bytes_in"}  # nothing that holds an address
    engine = create_db_engine(api.settings.database_path)
    try:
        recorder = Recorder(lambda r: httpx2.Response(200, content=b"x" * 123))
        fetcher = GuardedFetcher(
            FakeResolver({"files.example.org": ["93.184.216.34"]}),
            transport=recorder.transport,
            traffic=SqlTrafficCounter(engine),
            traffic_source="source-one",
        )

        async def go() -> None:
            await fetcher.get(
                f"https://files.example.org/{SECRET_CANARY}/ch-1?token={SECRET_CANARY}"
            )
            await fetcher.get("https://files.example.org/other")

        asyncio.run(go())
    finally:
        engine.dispose()
    rows = _counter_rows(api)
    assert len(rows) == 1  # one source, one day
    day, source, requests, received = rows[0]
    assert (source, requests, received) == ("source-one", 2, 246)
    assert str(day) == datetime.now(UTC).date().isoformat()  # UTC days, like every window
    dump = json.dumps([str(c) for c in rows])
    for marker in (SECRET_CANARY, "files.example.org", "93.184.216.34", "/other", "token"):
        assert marker not in dump


@pytest.mark.req("EF-INT-020", "ENF-SEC-013")
def test_traffic_counter_sits_after_the_ssrf_guard(api: AuthEnv) -> None:
    """A request the guard refuses never reached the network: it is not traffic and is not counted;
    the guard itself is where it was (the counter is a hook after it)."""
    engine = create_db_engine(api.settings.database_path)
    try:
        recorder = Recorder()
        fetcher = GuardedFetcher(
            FakeResolver({"inside.example.org": ["10.0.0.5"]}),
            transport=recorder.transport,
            traffic=SqlTrafficCounter(engine),
            traffic_source="source-one",
        )
        with pytest.raises(SsrfBlocked):
            asyncio.run(fetcher.get("https://inside.example.org/x"))
        assert recorder.requests == []
    finally:
        engine.dispose()
    assert _counter_rows(api) == []


@pytest.mark.req("EF-INT-020")
def test_a_fetcher_without_a_counter_is_unchanged(api: AuthEnv) -> None:
    recorder = Recorder()
    fetcher = GuardedFetcher(
        FakeResolver({"files.example.org": ["93.184.216.34"]}), transport=recorder.transport
    )
    assert asyncio.run(fetcher.get("https://files.example.org/x")).status == 200
    assert _counter_rows(api) == []


@pytest.mark.req("EF-INT-020")
def test_the_counter_failing_never_fails_the_request(api: AuthEnv) -> None:
    """A page on a dashboard is not worth a download: a counter that cannot write is logged only."""

    class Broken:
        def record(self, source_key: str, *, requests: int, bytes_in: int) -> None:
            raise sqlite3.OperationalError("database is locked")

    fetcher = GuardedFetcher(
        FakeResolver({"files.example.org": ["93.184.216.34"]}),
        transport=Recorder().transport,
        traffic=Broken(),
        traffic_source="source-one",
    )
    assert asyncio.run(fetcher.get("https://files.example.org/x")).status == 200
