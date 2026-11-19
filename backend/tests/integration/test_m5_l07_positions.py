"""M5 L07 (EF-LEC-007, EF-LEC-014, EF-USR-006, ENF-SEC-010): the positions of the reader.

`PUT /api/reader/positions` writes the anchor `reading_position` of the integrated reader (locator
v1 of ADR-0055) on a kept copy (`content`) or on an `export_item`; the progress of a Unit comes
from it (`reading_progress`). The real application and real sessions. The attacks: an excerpt, a
child on a hidden Work, another profile's carrier, a key, a device token, a clock set in the future.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.content_trust import CONTENT_FILTER
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, reading, storage
from shelfhold.reading.public import progress_of
from tests.auth_env import CSRF_HEADER
from tests.content_env import session_id_of, user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives, book, pages_of, unit_of

HASH = "sha256:" + "c" * 64
FILTER = CONTENT_FILTER


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def kept(world: World, *, edition: str = "ok", fmt: str = "cbz") -> tuple[str, str]:
    """A kept copy with a hash and the coverage of its Unit; returns (content id, unit id)."""
    content = book(world, pages_of(30), edition=edition, fmt=fmt)
    unit = unit_of(world, content)
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(storage.content).where(storage.content.c.id == content)
            .values(content_hash=HASH, content_hash_by="norm@1")
        )  # fmt: skip
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="content", carrier_id=content, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip
    return content, unit


def page(index: int, count: int = 30) -> dict[str, Any]:
    return {"v": 1, "type": "page", "index": index, "count": count}


def cfi(progression: float, version: str = FILTER) -> dict[str, Any]:
    return {
        "v": 1, "type": "cfi", "cfi": "epubcfi(/6/4!/4/2/1:0)", "totalProgression": progression,
        "filter": version,
    }  # fmt: skip


def stamp(world: World, minutes: float = 0) -> str:
    """A date relative to the application's own clock (the tests move it by hand)."""
    return (world.env.clock.now() + timedelta(minutes=minutes)).isoformat()


def put(
    world: World, client: Any, carrier: str, locator: dict[str, Any], *, minutes: float = 0,
    device: str | None = "phone", carrier_type: str = "content", **extra: Any,
) -> Any:  # fmt: skip
    body = {
        "carrier_type": carrier_type, "carrier_id": carrier, "locator": locator,
        "client_ts": stamp(world, minutes), "device_id": device, **extra,
    }  # fmt: skip
    return client.put(
        "/api/reader/positions", json=body, headers={CSRF_HEADER: world.env.csrf(client)}
    )


def current(client: Any, carrier: str, carrier_type: str = "content") -> Any:
    return client.get(f"/api/reader/{carrier_type}/{carrier}/position")


def history(client: Any, carrier: str, **params: Any) -> Any:
    return client.get(f"/api/reader/content/{carrier}/positions", params=params)


def count(world: World, table: sa.Table) -> int:
    with world.seed.engine.connect() as connection:
        return int(connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())


def conflicts(world: World) -> int:
    return count(world, reading.reading_conflict)


@pytest.mark.req("EF-LEC-007", "EF-LEC-014")
def test_locator_progress_lww(world: World) -> None:
    """The reader is where its latest position says, even if that is behind the furthest; the
    history keeps both; the progress of the Unit is recomputed from the anchors, never sent."""
    content, unit = kept(world)
    first = put(world, world.admin, content, page(20), minutes=-10)
    assert first.status_code == 200, first.text
    back = put(world, world.admin, content, page(4), minutes=-5)
    assert back.status_code == 200
    got = current(world.admin, content).json()
    assert got["position"]["locator"] == {**page(4), "page": 5}  # the latest, not the furthest
    assert got["position"]["id"] == back.json()["position"]["id"]
    assert got["conflict"] is None  # one device: going back is not a conflict
    listed = history(world.admin, content).json()["items"]
    assert [i["locator"]["index"] for i in listed] == [4, 20]  # newest first, both kept
    with world.seed.engine.connect() as connection:
        shown = progress_of(
            connection, viewer_id=user_id_of(world.seed.engine, "admin"),
            subject_id=user_id_of(world.seed.engine, "admin"), unit_ids=[unit],
        )  # fmt: skip
    assert [(p.unit_id, p.state) for p in shown] == [(unit, "in_progress")]  # never back to unread
    assert shown[0].last_position_id == first.json()["position"]["id"]  # the furthest
    assert shown[0].latest_position_id == back.json()["position"]["id"]  # the latest
    assert put(world, world.admin, content, {"cfi": "x"}).status_code == 422
    assert put(world, world.admin, content, page(2), device="x" * 200).status_code == 422


@pytest.mark.req("EF-LEC-007")
def test_offline_position_merge_by_bounded_client_ts(world: World) -> None:
    """A position written offline reaches the server late: it is dated by when it was made
    (`client_ts`), not when it arrived, and never later than the server's own date."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(10), minutes=-60).status_code == 200
    late = put(world, world.admin, content, page(25), minutes=-120)  # made earlier, arrives later
    assert late.status_code == 200
    assert current(world.admin, content).json()["position"]["locator"]["index"] == 10
    ahead = put(world, world.admin, content, page(7), minutes=3)  # a clock a little ahead
    assert ahead.status_code == 200
    stored = ahead.json()["position"]
    assert datetime.fromisoformat(stored["client_ts"]) <= world.env.clock.now()  # bounded
    assert current(world.admin, content).json()["position"]["locator"]["index"] == 7
    with world.seed.engine.connect() as connection:
        rows = connection.execute(
            sa.select(reading.reading_position.c.client_ts, reading.reading_position.c.server_ts)
        ).all()
    assert len(rows) == 3 and all(r.client_ts <= r.server_ts for r in rows)


@pytest.mark.req("EF-LEC-007")
def test_future_client_ts_rejected(world: World) -> None:
    content, _unit = kept(world)
    for minutes in (60, 24 * 60, 10**6):
        refused = put(world, world.admin, content, page(3), minutes=minutes)
        assert refused.status_code == 422 and refused.json()["code"] == "client_ts_in_future"
    naive = world.admin.put(
        "/api/reader/positions",
        json={"carrier_type": "content", "carrier_id": content, "locator": page(3),
              "client_ts": "2026-11-18T12:00:00"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )  # fmt: skip
    assert naive.status_code == 422  # a date without a zone says nothing
    assert count(world, reading.reading_position) == 0
    assert put(world, world.admin, content, page(3), minutes=3).status_code == 200  # skew allowed


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
def test_reading_position_on_unexported_content_anchor(world: World) -> None:
    """A Work that is not exported is read from its kept copy: the anchor is a `content`, written
    as a device position that marks nothing read by itself, and the Unit's progress follows."""
    content, unit = kept(world)
    got = put(world, world.admin, content, page(29), device="tablet")
    assert got.status_code == 200
    with world.seed.engine.connect() as connection:
        row = connection.execute(sa.select(reading.reading_position)).one()
    assert (row.carrier_type, row.carrier_id, row.content_hash) == ("content", content, HASH)
    assert (row.source, row.device_id, row.read_units) == ("device", "tablet", [])
    assert row.percent == 1.0 and row.locator == {**page(29), "page": 30}
    assert count(world, export.export_item) == 0  # no export_item was needed
    admin = user_id_of(world.seed.engine, "admin")
    with world.seed.engine.connect() as connection:
        (shown,) = progress_of(connection, viewer_id=admin, subject_id=admin, unit_ids=[unit])
    assert shown.state == "in_progress"  # a look at the last page is a position, not a reading
    epub, _unit = kept(world, fmt="epub")
    assert put(world, world.admin, epub, cfi(0.4)).status_code == 200
    with world.seed.engine.connect() as connection:
        last = connection.execute(
            sa.select(reading.reading_position).where(reading.reading_position.c.carrier_id == epub)
        ).one()
    assert last.percent == 0.4 and last.read_units == [] and last.locator == cfi(0.4)


@pytest.mark.req("EF-LEC-007")
def test_conflict_proposed_once_above_threshold(world: World) -> None:
    """Two devices that diverge: the latest wins, and when the other is further by more than D8's
    threshold the reader is asked once (`reading_conflict`) until she chooses."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(20, 50), device="tab", minutes=-30).is_success
    raised = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10)
    proposal = raised.json()["conflict"]
    assert proposal is not None
    assert proposal["winner"]["locator"]["index"] == 2
    assert proposal["loser"]["locator"]["index"] == 20  # « Reprendre à la page 3 ou 21 ? »
    again = put(world, world.admin, content, page(3, 50), device="phone", minutes=-5)
    assert again.json()["conflict"]["id"] == proposal["id"]  # proposed once
    assert current(world.admin, content).json()["conflict"]["id"] == proposal["id"]
    assert conflicts(world) == 1
    # the reader resumes where the other device was: that becomes the current position
    done = world.admin.post(
        f"/api/reader/conflicts/{proposal['id']}/resolve",
        json={"choice": "loser", "device_id": "phone"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert done.status_code == 200, done.text
    assert done.json()["conflict"] is None and done.json()["position"]["locator"]["index"] == 20
    after = put(world, world.admin, content, page(21, 50), device="phone", minutes=0)
    assert after.json()["conflict"] is None and conflicts(world) == 1  # nothing to propose
    twice = world.admin.post(
        f"/api/reader/conflicts/{proposal['id']}/resolve",
        json={"choice": "winner"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert twice.status_code == 404  # settled: nothing left to choose


@pytest.mark.req("EF-LEC-007")
def test_conflict_kept_winner_is_not_proposed_again(world: World) -> None:
    """Keeping the latest dismisses what the other device had said until it speaks again."""
    content, _unit = kept(world, fmt="epub")
    assert put(world, world.admin, content, cfi(0.60), device="tab", minutes=-30).is_success
    proposal = put(world, world.admin, content, cfi(0.10), device="phone", minutes=-10).json()
    assert proposal["conflict"] is not None
    kept_winner = world.admin.post(
        f"/api/reader/conflicts/{proposal['conflict']['id']}/resolve",
        json={"choice": "winner"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert kept_winner.status_code == 200 and kept_winner.json()["conflict"] is None
    world.env.clock.advance(timedelta(minutes=1))  # what comes next is after her choice
    assert put(world, world.admin, content, cfi(0.12), device="phone", minutes=-2).is_success
    assert conflicts(world) == 1  # the tablet's old word is dismissed
    assert put(world, world.admin, content, cfi(0.80), device="tab", minutes=-1).is_success
    assert put(world, world.admin, content, cfi(0.15), device="phone", minutes=0).is_success
    assert conflicts(world) == 2  # the tablet spoke again: it is a new divergence


@pytest.mark.req("EF-LEC-007")
@pytest.mark.parametrize(
    ("kind", "far", "near", "proposed"),
    [
        ("epub", 0.52, 0.50, False),  # 0.02 is not above
        ("epub", 0.53, 0.50, True),  # D8: `totalProgression` alone, no page count in reflow
        ("pages", 11, 9, False),  # 4 % of 50 pages, but 2 pages
        ("pages", 12, 9, True),  # 6 % and 3 pages
    ],
)
def test_conflict_threshold_per_kind(
    world: World, kind: str, far: float, near: float, proposed: bool
) -> None:
    content, _unit = kept(world, fmt="epub" if kind == "epub" else "cbz")
    make = (lambda v: cfi(v)) if kind == "epub" else (lambda v: page(int(v), 50))
    assert put(world, world.admin, content, make(far), device="tab", minutes=-20).is_success
    got = put(world, world.admin, content, make(near), device="phone", minutes=-10)
    assert (got.json()["conflict"] is not None) is proposed
    assert conflicts(world) == int(proposed)


@pytest.mark.req("EF-LEC-007")
def test_locator_restored_across_filter_versions(world: World) -> None:
    """ADR-0055 (N2, I30): a position written under another version of the filter is restored as it
    was written (never moved in silence) and marked `approximate`; under the current one, exact."""
    content, _unit = kept(world, fmt="epub")
    old = "content-filter@0"
    assert put(world, world.admin, content, cfi(0.42, old), minutes=-5).is_success
    got = current(world.admin, content).json()["position"]
    assert got["locator"] == cfi(0.42, old) and got["approximate"] is True
    assert put(world, world.admin, content, cfi(0.44), minutes=-1).is_success
    got = current(world.admin, content).json()["position"]
    assert got["locator"] == cfi(0.44) and got["approximate"] is False
    listed = history(world.admin, content).json()["items"]
    assert [i["approximate"] for i in listed] == [False, True]
    # the content under it was replaced: restored too, and approximate
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(storage.content).where(storage.content.c.id == content)
            .values(content_hash="sha256:" + "d" * 64)
        )  # fmt: skip
    assert current(world.admin, content).json()["position"]["approximate"] is True


@pytest.mark.req("EF-LEC-007", "EF-LEC-014", "ENF-SEC-010")
def test_excerpt_writes_no_position(world: World) -> None:
    """The excerpt is read without leaving a trace: no position, no history, no conflict. A call
    that says `excerpt`, one that carries an excerpt ticket and a carrier that can only be read as
    an excerpt (an exported file) are all refused, whatever the client claims."""
    content, unit = kept(world)
    refused = put(world, world.admin, content, page(3), scope="excerpt")
    assert refused.status_code == 409 and refused.json()["code"] == "excerpt_writes_no_position"
    user = user_id_of(world.seed.engine, "admin")
    issued = world.access.issue(
        user_id=user, session_id=session_id_of(world.seed.engine, "admin"),
        carrier_type="content", carrier_id=content, scope="excerpt",
    )  # fmt: skip
    with_ticket = put(world, world.admin, content, page(3), ticket=issued.token)
    assert with_ticket.status_code == 409
    assert with_ticket.json()["code"] == "excerpt_writes_no_position"
    full = world.access.issue(
        user_id=user, session_id=session_id_of(world.seed.engine, "admin"),
        carrier_type="content", carrier_id=content,
    )  # fmt: skip
    assert put(world, world.admin, content, page(3), ticket=full.token).is_success  # full: fine
    with write_transaction(world.seed.engine) as connection:
        connection.execute(sa.delete(reading.reading_position))
    item = world.export_item(world.seed.editions["ok"], content)
    world.src.item_content[item] = content
    named = put(world, world.admin, item, page(3), carrier_type="export_item")
    assert named.status_code == 409 and named.json()["code"] == "excerpt_writes_no_position"
    assert count(world, reading.reading_position) == 0 and conflicts(world) == 0
    assert history(world.admin, content).json()["items"] == []
    with world.seed.engine.connect() as connection:
        assert progress_of(connection, viewer_id=user, subject_id=user, unit_ids=[unit]) == []
    # a ticket that is not this profile's, or not for this carrier, opens nothing
    other = world.access.issue(
        user_id=user_id_of(world.seed.engine, "mum"),
        session_id=session_id_of(world.seed.engine, "mum"),
        carrier_type="content", carrier_id=content, scope="excerpt",
    )  # fmt: skip
    assert put(world, world.admin, content, page(3), ticket=other.token).status_code == 422
    assert put(world, world.admin, content, page(3), ticket="garbage").status_code == 422
    assert count(world, reading.reading_position) == 0


@pytest.mark.req("EF-LEC-014", "EF-EXP-026")
def test_excerpt_never_pushed_to_kavita(world: World) -> None:
    """What the excerpt did not write cannot be sent: the pass of the progress synchronisation
    finds nothing to say. And a reader's EPUB position (a `cfi`) is never a page to push, on any
    carrier, until L09 says how (the filter that holds the push back)."""
    from shelfhold.reading.application import sync

    content, _unit = kept(world, fmt="epub")
    assert put(world, world.admin, content, cfi(0.5), scope="excerpt").status_code == 409
    assert count(world, reading.reading_position) == 0
    position = reading.reading_position
    item = world.export_item(world.seed.editions["ok"], content)
    user = user_id_of(world.seed.engine, "admin")
    with write_transaction(world.seed.engine) as connection:
        for locator, percent in ((cfi(0.5), 0.5), ({"page": 3}, 0.1)):
            connection.execute(
                sa.insert(position).values(
                    id=new_ulid(), user_id=user, carrier_type="export_item", carrier_id=item,
                    content_hash=HASH, locator=locator, percent=percent,
                    client_ts=datetime.now(UTC), device_id="d", source="device",
                )
            )  # fmt: skip
    with world.seed.engine.connect() as connection:
        rows = connection.execute(
            sa.select(position.c.locator).where(sync.relayable_position(position))
        ).all()
    assert [r.locator for r in rows] == [{"page": 3}]  # the EPUB anchor is held back


@pytest.mark.req("EF-USR-006", "ENF-SEC-010", "EF-LEC-014")
def test_member_cannot_write_position_for_another_profile(world: World) -> None:
    """IDOR: the profile is the session's, never a field of the request; another profile's
    history, conflict and position are not hers to read or settle."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(20, 50), device="tab", minutes=-30).is_success
    proposal = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10).json()
    admin = user_id_of(world.seed.engine, "admin")
    mum = user_id_of(world.seed.engine, "mum")
    smuggled = put(world, world.mum, content, page(5), user_id=admin, subject_id=admin)
    assert smuggled.status_code == 422  # an unknown field never chooses the profile
    assert count(world, reading.reading_position) == 2  # nothing was written
    assert put(world, world.mum, content, page(5), device="mums").is_success
    with world.seed.engine.connect() as connection:
        owners = sorted(
            (r.user_id, r.device_id)
            for r in connection.execute(
                sa.select(reading.reading_position.c.user_id, reading.reading_position.c.device_id)
            )
        )
    assert owners == sorted([(admin, "tab"), (admin, "phone"), (mum, "mums")])
    seen = current(world.mum, content).json()
    assert seen["conflict"] is None
    assert [i["device_id"] for i in history(world.mum, content).json()["items"]] == ["mums"]
    stolen = world.mum.post(
        f"/api/reader/conflicts/{proposal['conflict']['id']}/resolve",
        json={"choice": "loser"},
        headers={CSRF_HEADER: world.env.csrf(world.mum)},
    )
    missing = world.mum.post(
        f"/api/reader/conflicts/{new_ulid()}/resolve",
        json={"choice": "loser"},
        headers={CSRF_HEADER: world.env.csrf(world.mum)},
    )
    assert stolen.status_code == 404 == missing.status_code
    assert (stolen.json()["code"], len(stolen.content)) == (
        missing.json()["code"], len(missing.content),
    )  # fmt: skip
    assert current(world.admin, content).json()["conflict"]["id"] == proposal["conflict"]["id"]


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_child_position_on_hidden_work_refused(world: World) -> None:
    """A child writes nothing and reads nothing on a Work above her ceiling: the answer is the one
    of a carrier that does not exist."""
    hidden, _unit = kept(world, edition="twelve")
    shown, _unit = kept(world, edition="ok")
    absent = put(world, world.kid, new_ulid(), page(1))
    refused = put(world, world.kid, hidden, page(1))
    assert refused.status_code == 404 == absent.status_code
    assert (refused.json()["code"], len(refused.content)) == (
        absent.json()["code"], len(absent.content),
    )  # fmt: skip
    assert count(world, reading.reading_position) == 0
    assert current(world.kid, hidden).status_code == 404
    assert history(world.kid, hidden).status_code == 404
    assert put(world, world.kid, shown, page(1)).status_code == 200  # her own ceiling is fine


@pytest.mark.req("EF-USR-006", "ENF-SEC-010", "EF-LEC-014")
def test_reader_positions_credentials_and_identifiers(world: World) -> None:
    """The credential is the session cookie: a key of the API, a device token and no session at
    all write nothing; bad identifiers are an absent thing; the CSRF token is required."""
    from shelfhold.identity.public import local_auth

    content, _unit = kept(world)
    auth = local_auth(world.seed.engine, world.env.settings)
    key = auth.create_api_key(name="reader", username="admin", read_only=False).value
    bare = world.env.other_client("10.0.0.9")
    body = {"carrier_type": "content", "carrier_id": content, "locator": page(1),
            "client_ts": stamp(world)}  # fmt: skip
    got = bare.put("/api/reader/positions", json=body, headers={"Authorization": f"Bearer {key}"})
    assert got.status_code in {401, 403}
    created = auth.create_device_token(subject_username="mum", name="liseuse")
    raw = base64.b64encode(f"{created.public_id}:{created.secret}".encode()).decode()
    other = world.env.other_client("10.0.0.8")
    got = other.put("/api/reader/positions", json=body, headers={"Authorization": f"Basic {raw}"})
    assert got.status_code in {401, 403}
    anonymous = world.env.other_client("10.0.0.7")
    assert anonymous.put("/api/reader/positions", json=body).status_code in {401, 403}
    no_csrf = world.admin.put("/api/reader/positions", json=body)
    assert no_csrf.status_code == 403
    for carrier in ("not-an-id", "01HZZZZZZZZZZZZZZZZZZZZZZZ"):
        got = put(world, world.admin, carrier, page(1))
    assert got.status_code == 404, got.text
    assert put(world, world.admin, content, page(1), carrier_type="nothing").status_code in {
        404,
        422,
    }
    assert count(world, reading.reading_position) == 0


@pytest.mark.req("EF-LEC-007")
def test_overtaken_proposal_is_closed_and_refused_without_writing(world: World) -> None:
    """A proposal that a later position overtook is no longer shown; answering it is a typed 409
    that writes nothing; the next divergence may be proposed again."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(20, 50), device="tab", minutes=-30).is_success
    first = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10).json()
    assert first["conflict"] is not None
    assert put(world, world.admin, content, page(40, 50), device="tab", minutes=-5).is_success
    assert current(world.admin, content).json()["conflict"] is None  # overtaken: not proposed
    before = count(world, reading.reading_position)
    stale = world.admin.post(
        f"/api/reader/conflicts/{first['conflict']['id']}/resolve",
        json={"choice": "loser", "device_id": "phone"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "conflict_stale"
    assert count(world, reading.reading_position) == before  # nothing written
    # the phone is behind again: a new proposal, the stale one having been closed
    again = put(world, world.admin, content, page(3, 50), device="phone", minutes=-1).json()
    assert again["conflict"] is not None and again["conflict"]["id"] != first["conflict"]["id"]
    assert again["conflict"]["loser"]["locator"]["index"] == 40


@pytest.mark.req("EF-LEC-007")
def test_dates_that_cannot_be_a_reading_are_a_typed_422(world: World) -> None:
    content, _unit = kept(world)
    for text in (
        "1999-12-31T23:59:59+00:00",
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-14:00",
    ):
        got = world.admin.put(
            "/api/reader/positions",
            json={"carrier_type": "content", "carrier_id": content, "locator": page(3),
                  "client_ts": text},
            headers={CSRF_HEADER: world.env.csrf(world.admin)},
        )  # fmt: skip
        assert got.status_code == 422, (text, got.status_code)
        assert got.json()["code"] in {"client_ts_out_of_range", "client_ts_in_future"}, text
    assert count(world, reading.reading_position) == 0


@pytest.mark.req("EF-LEC-007")
def test_an_answer_never_installs_a_position_older_than_the_current_one(world: World) -> None:
    """The winning device goes on reading, the proposal stays open and follows it; the other
    device's place is then chosen (`loser`): the answer is a position written now, at the place
    that was proposed, never one older than the position she is on. And when the far device has
    spoken again meanwhile, the answer is refused."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(20, 50), device="tab", minutes=-30).is_success
    asked = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10).json()
    proposal = asked["conflict"]
    assert proposal is not None
    onward = put(world, world.admin, content, page(5, 50), device="phone", minutes=-5).json()
    assert onward["conflict"]["id"] == proposal["id"]  # the same proposal, the phone reads on
    assert onward["conflict"]["winner"]["id"] == onward["position"]["id"]  # and it follows her
    before = datetime.fromisoformat(onward["position"]["client_ts"])
    done = world.admin.post(
        f"/api/reader/conflicts/{proposal['id']}/resolve",
        json={"choice": "loser", "device_id": "phone"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert done.status_code == 200, done.text
    installed = done.json()["position"]
    assert installed["locator"]["index"] == 20  # the place that was proposed
    assert datetime.fromisoformat(installed["client_ts"]) >= before  # never older than hers
    assert current(world.admin, content).json()["position"]["id"] == installed["id"]
    listed = history(world.admin, content).json()["items"]
    assert listed[0]["id"] == installed["id"]  # the latest of the history
    world.env.clock.advance(timedelta(minutes=1))  # what follows is after her choice
    # the same, but the far device spoke again before the answer: refused, nothing written
    assert put(world, world.admin, content, page(30, 50), device="tab", minutes=1).is_success
    third = put(world, world.admin, content, page(4, 50), device="phone", minutes=2).json()
    assert third["conflict"] is not None
    assert put(world, world.admin, content, page(45, 50), device="tab", minutes=3).is_success
    written = count(world, reading.reading_position)
    refused = world.admin.post(
        f"/api/reader/conflicts/{third['conflict']['id']}/resolve",
        json={"choice": "loser", "device_id": "phone"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert refused.status_code == 409 and refused.json()["code"] == "conflict_stale"
    assert count(world, reading.reading_position) == written
