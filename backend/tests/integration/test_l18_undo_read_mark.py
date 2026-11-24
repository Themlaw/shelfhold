"""L18 (EF-LEC-014, EF-USR-006, EF-EXP-026, D21): undo a read mark.

A read mark is a journal line (`read_units` of the position that closed a plausible move), never
a row deleted: the undo adds a dated line of history (`undone_units`) and nothing else changes.
The member undoes their own mark; the administrator undoes that of a child profile, and nobody
else's — another member's mark answers exactly like one that is not there. The destination of the
profile (a Kavita that holds the member's key) is told « unread » for a chapter Shelfhold itself
pushed as read, and the next relay never puts the mark back from a position older than the undo.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import reading, storage
from shelfhold.reading.public import progress_of, undo_read_mark
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.sync_env import START, sync_env

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HASH = "sha256:" + "a" * 64


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    yield from _world(monkeypatch, settings)


def read_unit(world: World, edition: str = "ok") -> tuple[str, str]:
    """A Unit of the Edition and the carrier a reader walks on: `(unit_id, carrier_id)`."""
    from tests.content_env import add_carrier

    carrier = add_carrier(world.seed.engine, world.seed.editions[edition])
    with world.seed.engine.connect() as connection:
        unit = str(
            connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == carrier)
            ).scalar_one()
        )
    return unit, carrier


def mark_read(world: World, username: str, unit: str, carrier: str) -> None:
    """The journal line of a traversal: the position that closed a move over the Unit."""
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(reading.reading_position).values(
                id=new_ulid(), user_id=user_id_of(world.seed.engine, username),
                carrier_type="content", carrier_id=carrier, content_hash=HASH,
                locator={"page": 3}, percent=None, client_ts=T0, server_ts=T0,
                source="device", device_id="reader-1", covered_units=[unit],
                read_units=[unit],
            )
        )  # fmt: skip


def state(world: World, username: str, unit: str) -> str:
    who = user_id_of(world.seed.engine, username)
    with world.seed.engine.connect() as connection:
        found = progress_of(connection, viewer_id=who, subject_id=who, unit_ids=[unit])
    return found[0].state if found else "unread"


def rows_of(world: World, username: str) -> list[Any]:
    table = reading.reading_position
    with world.seed.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(table)
                .where(table.c.user_id == user_id_of(world.seed.engine, username))
                .order_by(table.c.server_ts, table.c.id)
            )
        )


def undo(world: World, client: Any, unit: str, *, for_child: str | None = None) -> Any:
    path = f"/api/reader/read-marks/{unit}/undo"
    if for_child is not None:
        path += f"/for-child/{for_child}"
    return world.env.post(path, client=client)


# -- the member, for themself -------------------------------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_member_undoes_own_read_mark(world: World) -> None:
    """The member's own mark, through the reader's own door: the Unit is no longer read, and a
    second undo answers nothing (there is nothing left to undo)."""
    unit, carrier = read_unit(world)
    mark_read(world, "kid", unit, carrier)
    mark_read(world, "mum", unit, carrier)
    assert state(world, "mum", unit) == "read"
    answer = undo(world, world.mum, unit)
    assert answer.status_code == 200
    assert answer.json() == {"undone": 1}
    assert state(world, "mum", unit) != "read"
    assert state(world, "kid", unit) == "read"  # the child's own mark was not touched
    again = undo(world, world.mum, unit)
    assert again.status_code == 200 and again.json() == {"undone": 0}


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_member_cannot_undo_another_profile_mark(world: World) -> None:
    """IDOR: a member's own door never names another profile, the for-child door is the
    administrator's, and even the administrator undoes no mark of another adult."""
    unit, carrier = read_unit(world)
    kid = user_id_of(world.seed.engine, "kid")
    mum = user_id_of(world.seed.engine, "mum")
    admin = user_id_of(world.seed.engine, "admin")
    mark_read(world, "kid", unit, carrier)
    # the member's own door undoes her own mark only: the child's is not hers to undo
    assert undo(world, world.mum, unit).json() == {"undone": 0}
    assert state(world, "kid", unit) == "read"
    # the for-child door refuses her outright
    refused = undo(world, world.mum, unit, for_child=kid)
    assert refused.status_code == 403
    assert state(world, "kid", unit) == "read"
    # and the domain door answers the same nothing for whoever is neither the subject nor the
    # administrator of a child
    with write_transaction(world.seed.engine) as connection:
        assert undo_read_mark(connection, viewer_id=mum, subject_id=kid, unit_id=unit) == 0
    assert state(world, "kid", unit) == "read"
    # the administrator's reach stops at child profiles: another adult's mark is not there
    mark_read(world, "mum", unit, carrier)
    with write_transaction(world.seed.engine) as connection:
        assert undo_read_mark(connection, viewer_id=admin, subject_id=mum, unit_id=unit) == 0
    assert state(world, "mum", unit) == "read"


@pytest.mark.req("EF-LEC-014")
def test_undo_read_mark_is_journaled_never_deleted(world: World) -> None:
    """The undo is a line of history: every position is still there, the journal line of the
    traversal is intact, and the undo is a dated entry of `undone_units` — nothing is deleted."""
    unit, carrier = read_unit(world)
    mark_read(world, "mum", unit, carrier)
    before = rows_of(world, "mum")
    answer = undo(world, world.mum, unit)
    assert answer.status_code == 200 and answer.json() == {"undone": 1}
    after = rows_of(world, "mum")
    assert len(after) == len(before)  # nothing was deleted, nothing was rewritten but the journal
    assert [r.read_units for r in after] == [[unit]]  # the traversal's line is intact
    (stamp,) = after[0].undone_units.keys()
    assert stamp == unit
    when = datetime.fromisoformat(after[0].undone_units[unit])
    assert when.tzinfo is not None  # a dated line of history, not a hole in the table
    assert state(world, "mum", unit) != "read"


# -- the administrator, for a child --------------------------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_admin_undoes_the_mark_of_a_child_profile(world: World) -> None:
    """The for-child door: the administrator undoes the mark of a child profile, journaled the
    same way, and the child keeps her own door for her own mark."""
    unit, carrier = read_unit(world)
    kid = user_id_of(world.seed.engine, "kid")
    mark_read(world, "kid", unit, carrier)
    answer = undo(world, world.admin, unit, for_child=kid)
    assert answer.status_code == 200
    assert answer.json() == {"undone": 1}
    assert state(world, "kid", unit) != "read"
    assert rows_of(world, "kid")[0].read_units == [unit]  # journalled, never deleted
    # a child's own mark is hers to undo too
    mark_read(world, "kid", unit, carrier)
    assert undo(world, world.kid, unit).json() == {"undone": 1}
    assert state(world, "kid", unit) != "read"


@pytest.mark.req("EF-LEC-014")
def test_the_journal_of_the_undo_says_who_and_whose(world: World) -> None:
    """The event of the undo says who undoes and whose the mark is: ids only, never a name."""
    unit, carrier = read_unit(world)
    kid = user_id_of(world.seed.engine, "kid")
    admin = user_id_of(world.seed.engine, "admin")
    mark_read(world, "kid", unit, carrier)
    with structlog.testing.capture_logs() as logs:
        assert undo(world, world.admin, unit, for_child=kid).json() == {"undone": 1}
    (event,) = [line for line in logs if line.get("event") == "reading.read_unmarked"]
    assert event["actor_id"] == admin  # who undoes
    assert event["user_id"] == kid  # whose the mark is
    assert event["unit"] == unit and event["marks"] == 1
    assert "kid" not in repr(event)  # a username never enters the journal


@pytest.mark.req("EF-LEC-014", "EF-USR-016")
def test_the_admin_undo_of_a_child_mark_is_a_line_of_the_audit_table(world: World) -> None:
    """Closure review (§3.9): the administrator's undo of a child's mark leaves a row in the audit
    table (who, whose), not only a line of the application journal. Her own undo writes none."""
    unit, carrier = read_unit(world)
    kid = user_id_of(world.seed.engine, "kid")
    admin = user_id_of(world.seed.engine, "admin")
    query = sa.text(
        "SELECT actor_id, target FROM audit WHERE action = 'reading.read_mark_undone_by_admin'"
    )
    mark_read(world, "kid", unit, carrier)
    assert undo(world, world.kid, unit).json() == {"undone": 1}  # her own: no audit line
    with world.seed.engine.connect() as connection:
        assert connection.execute(query).all() == []
    mark_read(world, "kid", unit, carrier)
    assert undo(world, world.admin, unit, for_child=kid).json() == {"undone": 1}
    with world.seed.engine.connect() as connection:
        rows = connection.execute(query).all()
    assert [(r.actor_id, r.target) for r in rows] == [(admin, kid)]


@pytest.mark.req("EF-LEC-014", "ENF-SEC-010")
def test_an_api_key_cannot_undo_a_read_mark(world: World) -> None:
    """A key of the API is not the reader's session: neither door of the undo answers it, and
    nothing is undone through one."""
    from shelfhold.identity.public import local_auth

    unit, carrier = read_unit(world)
    kid = user_id_of(world.seed.engine, "kid")
    mark_read(world, "mum", unit, carrier)
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="mumkey", username="mum", read_only=True)
        .value
    )
    client = world.env.other_client("10.0.0.9")
    headers = {"Authorization": f"Bearer {key}"}
    own = client.post(f"/api/reader/read-marks/{unit}/undo", headers=headers)
    child = client.post(f"/api/reader/read-marks/{unit}/undo/for-child/{kid}", headers=headers)
    assert own.status_code in {401, 403}
    assert child.status_code in {401, 403}
    assert state(world, "mum", unit) == "read"  # nothing was undone


# -- the screen's list ---------------------------------------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_read_marks_list_shows_only_what_the_caller_may_see(world: World) -> None:
    """The list of the screen: a profile's own marks, and for an administrator the marks of the
    child profiles; another member's marks are not there, a hidden Edition answers like one with
    no mark, and no key of the API reads the reader's screen."""
    from shelfhold.identity.public import local_auth

    unit, carrier = read_unit(world)
    mark_read(world, "mum", unit, carrier)
    mark_read(world, "kid", unit, carrier)
    edition = world.seed.editions["ok"]
    hidden = world.seed.editions["twelve"]

    def items(client: Any, asked: str) -> list[dict[str, Any]]:
        got = client.get("/api/reader/read-marks", params={"edition_id": asked})
        assert got.status_code == 200
        return got.json()["items"]

    mine = items(world.mum, edition)
    assert [(i["username"], i["unit_id"]) for i in mine] == [("mum", unit)]
    hers = items(world.kid, edition)
    assert [(i["username"], i["unit_id"]) for i in hers] == [("kid", unit)]
    # the administrator: the children's marks (and the administrator's own, here none); another
    # member's marks are never listed to the administrator
    every = items(world.admin, edition)
    assert [(i["username"], i["unit_id"]) for i in every] == [("kid", unit)]
    # an Edition the child may not see answers exactly like one with no mark
    assert items(world.kid, hidden) == []
    # a key of the API is not the reader's session
    key = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="mumkey", username="mum", read_only=True)
        .value
    )
    by_key = world.env.other_client("10.0.0.9").get(
        f"/api/reader/read-marks?edition_id={edition}", headers={"Authorization": f"Bearer {key}"}
    )
    assert by_key.status_code in {401, 403}


# -- the destination ------------------------------------------------------------------------------


@pytest.mark.req("EF-LEC-014", "EF-EXP-026")
def test_undo_propagates_unread_to_destination(tmp_path: Any) -> None:
    """A chapter Shelfhold itself pushed as read is pushed back as « unread » when the member
    undoes the mark; a chapter Kavita's own user read there is never undone by Shelfhold."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.reading.position(  # the end of a traversal that marked the Unit (a journal)
            reader, chapter.carrier, page=chapter.pages - 1, percent=1.0, source="device",
            content_hash=env.content_hash(chapter), client_ts=START, server_ts=START,
            read_units=chapter.units,
        )  # fmt: skip
        assert env.state(reader, chapter) == "read"
        own = env.chapter("Orchard Road", 2)
        env.read_in_kavita(reader, own, own.pages)  # Kavita's own user read it there
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == chapter.pages  # « read » was pushed
        env.clock.advance(minutes=1)
        with write_transaction(env.engine) as connection:
            assert (
                undo_read_mark(
                    connection,
                    viewer_id=reader,
                    subject_id=reader,
                    unit_id=chapter.units[0],
                    now=env.clock.now(),
                )
                == 1
            )
        assert env.state(reader, chapter) != "read"
        env.clock.advance(minutes=1)
        outcome = env.sync(reader)
        assert outcome.code == "ok"
        assert env.kavita_page(reader, chapter) == 0  # « non lu » reached the destination
        assert env.kavita_page(reader, own) == own.pages  # Kavita's own reading is never undone


@pytest.mark.req("EF-LEC-014", "EF-EXP-026")
def test_relay_does_not_remark_after_undo(tmp_path: Any) -> None:
    """Once the destination holds « unread », the next relay does not put the mark back from a
    position older than the undo: the journal line of the traversal marks nothing again, and the
    member's later report moves the page forward, never back to « read »."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.reading.position(
            reader, chapter.carrier, page=chapter.pages - 1, percent=1.0, source="device",
            content_hash=env.content_hash(chapter), client_ts=START, server_ts=START,
            read_units=chapter.units,
        )  # fmt: skip
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == chapter.pages
        env.clock.advance(minutes=1)
        with write_transaction(env.engine) as connection:
            undo_read_mark(
                connection,
                viewer_id=reader,
                subject_id=reader,
                unit_id=chapter.units[0],
                now=env.clock.now(),
            )
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 0
        env.clock.advance(minutes=1)
        env.sync(reader)  # the next relay: the old journal line marks nothing again
        assert env.kavita_page(reader, chapter) == 0
        # a later report of the member moves the page forward, never the mark
        env.clock.advance(minutes=5)
        env.device_position(reader, chapter, 3)
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 3
        assert env.state(reader, chapter) != "read"


@pytest.mark.req("EF-LEC-014", "EF-EXP-026")
def test_undo_of_a_read_the_destination_made_alone_keeps_its_page(tmp_path: Any) -> None:
    """A chapter only the destination read (a pulled server state, nothing ever pushed) keeps
    its page when the member undoes the mark: « non lu » is sent only for a chapter Shelfhold
    itself pushed as read."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        own = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, own, own.pages)  # the destination read it there, alone
        env.clock.advance(minutes=1)
        env.sync(reader)  # the pull records the server state: the mark exists, from it only
        assert env.state(reader, own) == "read"
        env.clock.advance(minutes=1)
        with write_transaction(env.engine) as connection:
            assert (
                undo_read_mark(
                    connection,
                    viewer_id=reader,
                    subject_id=reader,
                    unit_id=own.units[0],
                    now=env.clock.now(),
                )
                == 1
            )
        assert env.state(reader, own) != "read"
        env.clock.advance(minutes=1)
        outcome = env.sync(reader)
        assert outcome.code == "ok"
        assert env.kavita_page(reader, own) == own.pages  # the destination keeps its page
