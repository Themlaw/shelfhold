# ruff: noqa: F811  (the `world` fixture of the child suite is imported, then used as an argument)
"""M3 L12 (EF-INT-002): the typed SSE stream, its replay by `Last-Event-ID`, its heartbeat, its
`resync_required`, and what a child never learns from it (EF-USR-006, ENF-SEC-010).

Two levels. `Feed` (the stream's brain) is driven by hand with a test clock: every poll is one
instant, nothing sleeps. The route is driven through the real application (`open_stream`), with
the stream's wait replaced by a script.

The attacks on the child's filter are written from the spec: each "never" is a test that tries it.
"""

from __future__ import annotations

import asyncio
import base64
import re
import struct
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.interfaces.http_api.stream as stream_module
from shelfhold.events.activity import Viewer
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.events.stream import (
    BATCH,
    HEARTBEAT_S,
    RESYNC_FRAME,
    Cursor,
    Feed,
)
from shelfhold.events.upcast import upcast_payload
from shelfhold.identity.public import Credential, credential_is_live, local_auth, reader_scope
from shelfhold.infrastructure.event_sequence import invalidate_tokens
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.interfaces.http_api.lists import encode_since
from shelfhold.interfaces.http_api.stream import _frames, cursor_codec, frame_of
from shelfhold.jobs.retention import RetentionPolicy, expire_events_and_jobs
from tests.auth_env import T0, ManualClock
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.sse_env import cookie_header, open_stream

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))


def seal_cursor(key: bytes, user_id: str, cursor: Cursor) -> str:
    return cursor_codec(lambda: key, user_id)["seal"](cursor)


def open_cursor(key: bytes, user_id: str, token: str) -> Cursor | None:
    return cursor_codec(lambda: key, user_id)["unseal"](token)


SEE = ("EF-USR-006", "ENF-SEC-010")


# -- helpers ---------------------------------------------------------------------------------


def _insert(
    world: World, work: str, *, actor: str | None, visibility: str = "household",
    version: int = 1, payload: dict[str, Any] | None = None, kind: str = "work.updated",
    at: datetime = T0,
) -> int:  # fmt: skip
    body = payload if payload is not None else {"work_id": world.seed.works[work]}
    with world.seed.engine.begin() as connection:
        result = connection.execute(
            sa.insert(platform.event).values(
                type=kind,
                schema_version=version,
                payload=body,
                occurred_at=at,
                actor_id=actor,
                visibility=visibility,
            )
        )
        return int(result.inserted_primary_key[0])


def _admin_id(world: World) -> str:
    with world.seed.engine.connect() as connection:
        return str(connection.execute(sa.text("SELECT id FROM user WHERE role='admin'")).scalar())


def _feed(
    world: World, user_id: str, *, last_event_id: str | None = None,
    alive: Callable[[], bool] = lambda: True, key: bytes = KEY,
    upcast: Callable[..., dict[str, Any]] = upcast_payload,
) -> Feed:  # fmt: skip
    engine = world.seed.engine
    scope = reader_scope(engine, user_id)
    with engine.connect() as connection:
        role = connection.execute(
            sa.select(identity.user.c.role).where(identity.user.c.id == user_id)
        ).scalar_one()
    viewer = Viewer(user_id, str(role))

    def project(connection: Any, row: Any, payload: dict[str, Any]) -> Any:
        return frame_of(connection, viewer, scope, row, payload)

    return Feed(
        engine, scope=scope, project=project, clock=world.env.clock,
        **cursor_codec(lambda: key, user_id),
        alive=alive, scope_now=lambda: reader_scope(engine, user_id),
        last_event_id=last_event_id, upcast=upcast,
    )  # fmt: skip


def _started(world: World, user_id: str, **kwargs: Any) -> Feed:
    feed = _feed(world, user_id, **kwargs)
    assert feed.start() == []
    return feed


def _id_of(frame: str) -> str:
    match = re.search(r"^id: (s3\.\S+)$", frame, re.MULTILINE)
    assert match, frame
    return match.group(1)


def _works_of(frames: list[str]) -> list[str]:
    return re.findall(r'"work_id":"([^"]+)"', "".join(frames))


def _is_heartbeat(frame: str) -> bool:
    return ": keepalive" in frame and "event:" not in frame


def _advance(world: World, seconds: float) -> None:
    world.env.clock.advance(timedelta(seconds=seconds))


def _kinds(frames: list[str]) -> list[str]:
    """What an observer sees of a poll: the kind of each frame, and its length for a heartbeat
    (a payload's own length is the payload's business)."""
    return ["hb" if _is_heartbeat(f) else "event" for f in frames]


# -- replay, heartbeat, resync -----------------------------------------------------------------


@pytest.mark.req("EF-INT-002")
def test_sse_typed_last_event_id_replay(world: World) -> None:
    admin = _admin_id(world)
    live = _started(world, admin)
    for work in ("ok", "adult_shelf", "mixed"):
        _insert(world, work, actor=admin)
    frames = live.poll()
    assert len(frames) == 3
    assert all(f.startswith("id: s3.") and "\nevent: work.updated\ndata: {" in f for f in frames)
    assert _works_of(frames) == [world.seed.works[k] for k in ("ok", "adult_shelf", "mixed")]

    resumed = _started(world, admin, last_event_id=_id_of(frames[0]))
    assert resumed.poll() == [] or _works_of(resumed.poll()) == []  # drained by the first poll
    resumed = _feed(world, admin, last_event_id=_id_of(frames[0]))
    assert resumed.start() == []
    replay = resumed.poll()
    assert _works_of(replay) == _works_of(frames[1:])  # exactly what follows, in order
    assert [_id_of(f) for f in replay] != [_id_of(f) for f in frames[1:]]  # sealed anew each time


@pytest.mark.req("EF-INT-002")
def test_sse_frame_carries_the_type_and_no_row_number(world: World) -> None:
    admin = _admin_id(world)
    feed = _started(world, admin)
    row_id = _insert(world, "ok", actor=admin)
    (frame,) = feed.poll()
    assert '"type":"work.updated"' in frame and '"visibility":"household"' in frame
    assert '"event_id"' not in frame and f'"id":{row_id}' not in frame


@pytest.mark.req("EF-INT-002")
def test_sse_heartbeat_15s_no_buffering(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = _admin_id(world)
    feed = _started(world, admin)
    assert HEARTBEAT_S == 15.0
    assert feed.seconds_to_heartbeat() == pytest.approx(15.0)
    _advance(world, 14.9)
    assert feed.poll() == []  # not yet
    _advance(world, 0.1)
    (beat,) = feed.poll()
    assert _is_heartbeat(beat) and _id_of(beat)  # on time, and carrying a cursor
    _advance(world, 1000)  # a long stall is one beat, not a burst
    assert len(feed.poll()) == 1
    assert feed.poll() == []
    assert 0 < feed.seconds_to_heartbeat() <= 15.0

    # through the application: the stream waits the interval, then sends it, with the headers that
    # keep a proxy from holding it back
    received = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.env.client),
        until=lambda r: len(r.heartbeats()) >= 2,
    )  # fmt: skip
    assert received.status == 200
    assert received.headers["x-accel-buffering"] == "no"
    assert received.headers["content-type"].startswith("text/event-stream")
    assert "no-transform" in received.headers["cache-control"]
    assert received.waits[0] == pytest.approx(15.0)
    assert received.waits[1] == pytest.approx(15.0)


@pytest.mark.req("EF-INT-002")
def test_sse_resync_required_when_id_purged(world: World) -> None:
    """The retention job takes the oldest rows (and records how far): a cursor under that floor
    cannot be replayed from, one at or above it can."""
    admin = _admin_id(world)
    feed = _started(world, admin)
    late = T0 + timedelta(days=95)
    for _ in range(4):
        _insert(world, "ok", actor=admin)
    _insert(world, "ok", actor=admin, at=late)  # inside the retention: it stays
    tokens = [_id_of(f) for f in feed.poll()]
    expire_events_and_jobs(
        world.seed.engine, now=T0 + timedelta(days=100), policy=RetentionPolicy()
    )
    purged = _feed(world, admin, last_event_id=tokens[1])
    assert purged.start() == [RESYNC_FRAME]
    assert purged.closed and purged.poll() == []
    fresh = _feed(world, admin, last_event_id=tokens[4])  # at the floor or above: a replay
    assert fresh.start() == [] and not fresh.closed


@pytest.mark.req("EF-INT-002", "EF-BIB-053")
def test_sse_cursor_from_before_a_work_purge_is_resync(world: World) -> None:
    """The purge of a Work cuts every token issued before it (L11): the stream follows, and a
    cursor issued after the purge is good."""
    admin = _admin_id(world)
    feed = _started(world, admin)
    _insert(world, "ok", actor=admin)
    before = _id_of(feed.poll()[0])
    with world.seed.engine.begin() as connection:
        invalidate_tokens(connection)
    assert _feed(world, admin, last_event_id=before).start() == [RESYNC_FRAME]
    after = _started(world, admin)
    _advance(world, 15)
    assert _feed(world, admin, last_event_id=_id_of(after.poll()[0])).start() == []


@pytest.mark.req("EF-INT-002")
def test_sse_resync_frame_resets_the_browser_id(world: World) -> None:
    """`id:` with no value empties the browser's last id: it will not reconnect with the stale one
    in a loop."""
    admin = _admin_id(world)
    (frame,) = _feed(world, admin, last_event_id="nonsense").start()
    assert frame == "id:\nevent: resync_required\ndata: {}\n\n"


# -- a cursor that cannot be trusted ----------------------------------------------------------


def _valid_cursor(world: World, user_id: str) -> str:
    feed = _started(world, user_id)
    _insert(world, "ok", actor=user_id)
    return _id_of(feed.poll()[0])


def _tamper(token: str) -> str:
    return token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]


FORGED = {
    "empty": lambda w, u: "",
    "plain_number": lambda w, u: "3",
    "negative": lambda w, u: "-1",
    "wrong_prefix": lambda w, u: "e1.MQ",
    "list_token_of_the_same_profile": lambda w, u: encode_since(
        1, key=KEY, kind="works", scope=255
    ),
    "bare_prefix": lambda w, u: "s3.",
    "not_base64": lambda w, u: "s3.!!!!!!!!",
    "huge": lambda w, u: "s3." + "A" * 5000,
    "non_ascii": lambda w, u: "s3.é",
    "tampered": lambda w, u: _tamper(_valid_cursor(w, u)),
    "truncated": lambda w, u: _valid_cursor(w, u)[:-6],
    "other_key": lambda w, u: seal_cursor(OTHER_KEY, u, Cursor(1, 0, b"\0" * 6, 255)),
}


@pytest.mark.req("EF-INT-002")
@pytest.mark.parametrize("name", sorted(FORGED))
def test_sse_forged_last_event_id_is_resync_never_an_error(world: World, name: str) -> None:
    admin = _admin_id(world)
    feed = _feed(world, admin, last_event_id=FORGED[name](world, admin))
    assert feed.start() == [RESYNC_FRAME]  # the same frame, whatever the reason
    assert feed.closed


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_another_profiles_cursor_is_resync(world: World) -> None:
    admin, kid = _admin_id(world), world.kid_id
    admins = _valid_cursor(world, admin)
    kids = _valid_cursor(world, kid)
    assert _feed(world, kid, last_event_id=admins).start() == [RESYNC_FRAME]
    assert _feed(world, admin, last_event_id=kids).start() == [RESYNC_FRAME]
    assert _feed(world, kid, last_event_id=kids).start() == []  # her own opens


@pytest.mark.req("EF-INT-002")
def test_sse_cursor_from_the_future_is_resync(world: World) -> None:
    admin = _admin_id(world)
    _insert(world, "ok", actor=admin)
    with world.seed.engine.connect() as connection:
        head = connection.execute(sa.select(sa.func.max(platform.event.c.id))).scalar_one()
        epoch = connection.execute(
            sa.select(sa.func.max(platform.instance.c.restore_epoch))
        ).scalar_one()
    forged = seal_cursor(KEY, admin, Cursor(head + 50, epoch or 0, b"\0" * 6, 255))
    assert _feed(world, admin, last_event_id=forged).start() == [RESYNC_FRAME]


@pytest.mark.req("EF-INT-002")
def test_sse_cursor_from_before_a_restore_is_resync(world: World) -> None:
    admin = _admin_id(world)
    token = _valid_cursor(world, admin)
    with world.seed.engine.begin() as connection:  # a restore or a rollback bumps the epoch
        connection.execute(sa.update(platform.instance).values(restore_epoch=3))
    assert _feed(world, admin, last_event_id=token).start() == [RESYNC_FRAME]


@pytest.mark.req("EF-INT-002")
def test_sse_cursor_over_another_history_is_resync(world: World) -> None:
    admin = _admin_id(world)
    token = _valid_cursor(world, admin)
    with world.seed.engine.begin() as connection:  # the table is append-only: a restore replaces it
        position = connection.execute(sa.select(sa.func.max(platform.event.c.id))).scalar_one()
        connection.execute(sa.delete(platform.event))
        connection.execute(
            sa.insert(platform.event).values(
                id=position, type="work.updated", schema_version=1, payload={"work_id": "other"},
                occurred_at=T0, actor_id=admin, visibility="household",
            )
        )  # fmt: skip
    assert _feed(world, admin, last_event_id=token).start() == [RESYNC_FRAME]


@pytest.mark.req("EF-INT-002", *SEE)
@pytest.mark.parametrize("ceiling", [4, 12])
def test_sse_cursor_after_her_ceiling_changed_is_resync(world: World, ceiling: int) -> None:
    token = _valid_cursor(world, world.kid_id)
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(identity.user).where(identity.user.c.id == world.kid_id).values(
                max_age_rating=ceiling
            )
        )  # fmt: skip
    assert _feed(world, world.kid_id, last_event_id=token).start() == [RESYNC_FRAME]


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_open_stream_ends_with_resync_when_her_ceiling_changes(world: World) -> None:
    feed = _started(world, world.kid_id)
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.update(identity.user).where(identity.user.c.id == world.kid_id).values(
                max_age_rating=16
            )
        )  # fmt: skip
    _insert(world, "above", actor=world.kid_id)  # visible at 16: she must reload, not be told
    assert feed.poll() == [RESYNC_FRAME] and feed.closed


@pytest.mark.req("EF-INT-002")
def test_sse_open_stream_ends_with_resync_on_a_restore(world: World) -> None:
    feed = _started(world, _admin_id(world))
    with world.seed.engine.begin() as connection:
        connection.execute(sa.update(platform.instance).values(restore_epoch=9))
    assert feed.poll() == [RESYNC_FRAME] and feed.closed


# -- the cursor ----------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-002")
def test_sse_cursor_is_sealed_opaque_and_bound_to_its_profile() -> None:
    cursor = Cursor(1234, 2, b"abcdef", 10)
    one, two = seal_cursor(KEY, "u1", cursor), seal_cursor(KEY, "u1", cursor)
    assert one != two  # a fresh nonce: two cursors say nothing of the distance between positions
    raw = base64.urlsafe_b64decode(one[3:] + "=" * (-len(one[3:]) % 4))
    assert struct.pack(">Q", 1234) not in raw and b"abcdef" not in raw
    assert open_cursor(KEY, "u1", one) == cursor
    assert open_cursor(KEY, "u2", one) is None
    assert open_cursor(OTHER_KEY, "u1", one) is None


# -- what a child never learns -------------------------------------------------------------------


def _hidden_and_visible(world: World, kid: str) -> None:
    """V1 H H H V2 H, all of the child's own household entries."""
    for work in ("ok", "above", "unrated", "x", "adult_shelf", "flagged"):
        _insert(world, work, actor=kid)


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_child_gets_no_event_about_a_hidden_work_live_or_replayed(world: World) -> None:
    kid = world.kid_id
    first = _started(world, kid)
    _hidden_and_visible(world, kid)
    live = first.poll()
    shown = [world.seed.works[k] for k in ("ok", "adult_shelf")]
    assert _works_of(live) == shown
    world.clean("".join(live))
    # the replay from before the first row: the filter runs again, with her profile
    before = _started(world, kid)  # head: after everything
    assert before.poll() == []
    token = _valid_cursor(world, kid)  # a cursor, then everything again
    with world.seed.engine.begin() as connection:
        connection.execute(sa.delete(platform.event))
    _insert(world, "ok", actor=kid)
    # a replay of rows written while she was away
    away = _started(world, kid)
    _hidden_and_visible(world, kid)
    replay = _feed(world, kid)
    assert replay.start() == []
    assert _works_of(away.poll()) == shown
    del token


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_child_cursor_replay_filters_what_came_after(world: World) -> None:
    kid = world.kid_id
    feed = _started(world, kid)
    _insert(world, "ok", actor=kid)
    token = _id_of(feed.poll()[0])
    _hidden_and_visible(world, kid)  # V H H H V H after her cursor
    replay = _feed(world, kid, last_event_id=token)
    assert replay.start() == []
    frames = replay.poll()
    assert _works_of(frames) == [world.seed.works[k] for k in ("ok", "adult_shelf")]
    world.clean("".join(frames))


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_hidden_events_leave_no_gap_in_the_identifiers(world: World) -> None:
    kid = world.kid_id
    feed = _started(world, kid)
    _hidden_and_visible(world, kid)
    frames = feed.poll()
    tokens = [_id_of(f) for f in frames]
    assert len(tokens) == 2 and len(set(map(len, tokens))) == 1
    key_cursors = [open_cursor(KEY, kid, t) for t in tokens]
    assert key_cursors[1] is not None and key_cursors[0] is not None
    assert key_cursors[1].seq - key_cursors[0].seq > 1  # the server knows the distance...
    for token, cursor in zip(tokens, key_cursors, strict=True):  # ...the reader cannot read it
        raw = base64.urlsafe_b64decode(token[3:] + "=" * (-len(token[3:]) % 4))
        assert cursor is not None and struct.pack(">Q", cursor.seq) not in raw
        assert not re.fullmatch(r"\d+", token)


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_hidden_event_does_not_move_the_heartbeat(world: World) -> None:
    """The beat is on the clock's schedule: a hidden event neither sends one, nor delays one, nor
    makes the poll that saw it say anything."""
    kid = world.kid_id
    feed = _started(world, kid)
    for at, work in ((5.0, "above"), (10.0, "unrated"), (14.0, "x")):
        _advance(world, at - (world.env.clock.current - T0).total_seconds())
        _insert(world, work, actor=kid)
        assert feed.poll() == []  # nothing: not a frame, not a beat
    assert feed.seconds_to_heartbeat() == pytest.approx(1.0)  # still due at 15
    _advance(world, 1.0)
    (beat,) = feed.poll()
    assert _is_heartbeat(beat)


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_what_an_observer_sees_is_the_same_with_or_without_hidden_events(
    world: World,
) -> None:
    """The whole trace (when, what kind) of a child's stream, with and without hidden events
    between the visible ones, is identical: nothing in it counts them."""
    kid = world.kid_id

    def trace(hidden: bool) -> list[tuple[float, list[str]]]:
        with world.seed.engine.begin() as connection:
            connection.execute(sa.delete(platform.event))
        world.env.clock.current = T0
        feed = _started(world, kid)
        out = []
        for second in range(0, 46):
            world.env.clock.current = T0 + timedelta(seconds=second)
            if second in (3, 20, 33):
                _insert(world, "ok", actor=kid)
            if hidden and second in (3, 7, 12, 14, 15, 20, 21, 29, 30, 33):
                _insert(world, "above", actor=kid)
            out.append((float(second), _kinds(feed.poll())))
        return out

    assert trace(hidden=True) == trace(hidden=False)


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_hidden_events_never_cause_resync_and_beats_all_carry_a_cursor(world: World) -> None:
    kid = world.kid_id
    feed = _started(world, kid)
    _insert(world, "ok", actor=kid)
    token = _id_of(feed.poll()[0])
    for work in ("above", "unrated", "x"):
        _insert(world, work, actor=kid)
    assert feed.poll() == []
    _advance(world, 15)
    beats = feed.poll()
    assert len(beats) == 1 and _id_of(beats[0])  # a cursor even when nothing visible moved
    quiet = _feed(world, kid)
    quiet.start()
    _advance(world, 15)
    assert _id_of(quiet.poll()[0])  # and the same shape when nothing hidden happened either
    # she comes back with the cursor of her last visible frame: no resync, and nothing hidden
    back = _feed(world, kid, last_event_id=token)
    assert back.start() == []
    assert back.poll() == []
    # the cursor of the beat moves her past the hidden rows: it opens too
    assert _feed(world, kid, last_event_id=_id_of(beats[0])).start() == []


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_the_reader_filters_not_the_emitter(world: World) -> None:
    """The profile that counts is the reader's: an adult's private entry about a visible Work is
    not hers, her own household entry is the admin's."""
    admin, kid = _admin_id(world), world.kid_id
    kid_feed, admin_feed = _started(world, kid), _started(world, admin)
    _insert(world, "ok", actor=admin, visibility="private")
    _insert(world, "adult_shelf", actor=kid, visibility="household")
    _insert(world, "mixed", actor=admin, visibility="ops")
    assert _works_of(kid_feed.poll()) == [world.seed.works["adult_shelf"]]
    shown = _works_of(admin_feed.poll())
    assert world.seed.works["adult_shelf"] in shown  # the admin sees hers (own and household)


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_child_never_sees_a_row_that_names_a_hidden_work_beside_a_visible_one(
    world: World,
) -> None:
    kid = world.kid_id
    feed = _started(world, kid)
    both = {"work_ids": [world.seed.works["ok"], world.seed.works["above"]]}
    _insert(world, "ok", actor=kid, payload=both)
    _insert(world, "ok", actor=kid, payload={"deep": {"a": {"b": {"c": {"d": {"work_id": "x"}}}}}})
    assert feed.poll() == []  # a nested name is read as hidden (fail closed)


@pytest.mark.req("EF-INT-002")
def test_sse_upcast_on_read_for_an_old_format(world: World) -> None:
    """A row of version 1 reaches the reader at version 2, and stays what it was in the table."""
    admin = _admin_id(world)
    work = world.seed.works["ok"]
    steps = {("work.updated", 1): lambda p: {"work_id": p["work"], "renamed": True}}
    feed = _started(world, admin, upcast=partial(upcast_payload, current=2, steps=steps))
    _insert(world, "ok", actor=admin, version=1, payload={"work": work})
    _insert(world, "ok", actor=admin, version=2, payload={"work_id": work})
    old, new = feed.poll()
    assert f'"payload":{{"work_id":"{work}","renamed":true}}' in old
    assert f'"payload":{{"work_id":"{work}"}}' in new
    with world.seed.engine.connect() as connection:
        stored = connection.execute(
            sa.select(platform.event.c.schema_version, platform.event.c.payload).order_by(
                platform.event.c.id
            )
        ).all()
    assert stored[0].schema_version == 1 and stored[0].payload == {"work": work}


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_a_row_of_an_unreadable_version_is_sent_to_nobody(world: World) -> None:
    admin = _admin_id(world)
    feed = _started(world, admin)
    _insert(world, "ok", actor=admin, version=99)
    _insert(world, "adult_shelf", actor=admin)
    assert _works_of(feed.poll()) == [world.seed.works["adult_shelf"]]


@pytest.mark.req("EF-INT-002")
def test_sse_long_backlog_is_read_in_batches(world: World) -> None:
    admin = _admin_id(world)
    feed = _started(world, admin)
    for _ in range(BATCH + 5):
        _insert(world, "ok", actor=admin)
    assert len(feed.poll()) == BATCH and feed.backlog
    assert len(feed.poll()) == 5 and not feed.backlog


# -- the credential ------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-002", "EF-USR-021")
def test_sse_a_revoked_credential_gets_nothing_more(world: World) -> None:
    admin = _admin_id(world)
    state = {"live": True}
    feed = _started(world, admin, alive=lambda: state["live"])
    _insert(world, "ok", actor=admin)
    state["live"] = False
    assert feed.poll() == [] and feed.closed  # the row was there, nothing left
    # revoked while a batch is read: the batch does not leave
    calls = iter([True, False])
    again = _started(world, admin, alive=lambda: next(calls))
    _insert(world, "ok", actor=admin)
    assert again.poll() == [] and again.closed


@pytest.mark.req("EF-INT-002", "EF-USR-021")
def test_sse_without_a_credential_there_is_no_stream(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    received = open_stream(world.env.app, world.env.clock, monkeypatch)
    assert received.status == 401 and received.complete
    assert not any("event:" in f for f in received.frames)
    forged = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie="__Host-sid=forged-session-value"
    )
    assert forged.status == 401


@pytest.mark.req("EF-INT-002", "EF-USR-021")
def test_sse_a_session_revoked_while_the_stream_is_open_ends_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kid = world.kid_id

    def revoke() -> None:
        with world.seed.engine.begin() as connection:
            connection.execute(
                sa.update(identity.session).where(identity.session.c.user_id == kid).values(
                    revoked_at=world.env.clock.now()
                )
            )  # fmt: skip
        _insert(world, "ok", actor=kid)  # an event the revoked session must not get

    received = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.kid),
        script=[lambda: None, revoke],
    )  # fmt: skip
    assert received.status == 200
    assert received.complete  # the application closed it: nobody asked
    assert received.events() == []
    assert len(received.heartbeats()) == 1  # the one before the revocation


@pytest.mark.req("EF-INT-002", "EF-USR-021")
def test_sse_a_session_past_its_absolute_expiry_ends_the_stream(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expire() -> None:
        with world.seed.engine.begin() as connection:
            connection.execute(
                sa.update(identity.session).values(absolute_expiry=world.env.clock.now())
            )

    received = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.env.client),
        script=[expire],
    )  # fmt: skip
    assert received.complete


# -- through the application ---------------------------------------------------------------------


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_route_serves_the_child_what_the_filter_lets_through(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kid = world.kid_id
    received = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.kid),
        script=[lambda: _hidden_and_visible(world, kid)],
        until=lambda r: len(r.heartbeats()) >= 1,
    )  # fmt: skip
    assert _works_of(received.frames) == [world.seed.works[k] for k in ("ok", "adult_shelf")]
    world.clean("".join(received.frames))


@pytest.mark.req("EF-INT-002", *SEE)
def test_sse_route_replays_after_last_event_id_with_the_readers_profile(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kid = world.kid_id
    first = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.kid),
        script=[lambda: _insert(world, "ok", actor=kid)],
        until=lambda r: bool(r.events()),
    )  # fmt: skip
    (token,) = first.event_ids()
    _hidden_and_visible(world, kid)
    again = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.kid),
        headers={"Last-Event-ID": token}, until=lambda r: len(r.events()) >= 2,
    )  # fmt: skip
    assert _works_of(again.frames) == [world.seed.works[k] for k in ("ok", "adult_shelf")]
    world.clean("".join(again.frames))
    # another profile's cursor, through the door: 200 and a resync, never an error
    foreign = open_stream(
        world.env.app, world.env.clock, monkeypatch, cookie=cookie_header(world.env.client),
        headers={"Last-Event-ID": token},
    )  # fmt: skip
    assert foreign.status == 200 and foreign.frames == [RESYNC_FRAME] and foreign.complete


@pytest.mark.req("EF-INT-002")
def test_sse_live_event_wakes_the_stream_through_the_bus_and_leaves_it_clean(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real wait (no script): a published event wakes the stream at once, and a stream that
    ends takes its subscription with it."""
    admin = _admin_id(world)
    bus = EventBus(ManualClock())
    feed = _feed(world, admin)

    async def scenario() -> list[str]:
        waiting = asyncio.Event()
        real_wait = stream_module._wait

        async def wait(wake: asyncio.Event, seconds: float) -> None:
            waiting.set()  # the head is read, the stream is parked: a row now is a live row
            await real_wait(wake, seconds)

        monkeypatch.setattr(stream_module, "_wait", wait)
        frames = _frames(feed, bus)
        pending = asyncio.ensure_future(anext(frames))
        await asyncio.wait_for(waiting.wait(), 10)
        assert len(bus._subscribers) == 1
        row = _insert(world, "ok", actor=admin)
        bus.deliver([PublishedEvent(row, "work.updated", {}, "household", ())])
        got = await asyncio.wait_for(pending, 10)  # woken, not timed out (15 s)
        await frames.aclose()
        assert len(bus._subscribers) == 0
        return [got]

    (frame,) = asyncio.run(scenario())
    assert world.seed.works["ok"] in frame


@pytest.mark.req("EF-INT-002")
def test_event_bus_subscribe_returns_an_idempotent_unsubscribe() -> None:
    bus = EventBus(ManualClock())
    seen: list[int] = []
    off = bus.subscribe(lambda e: seen.append(e.id))
    event = PublishedEvent(1, "work.updated", {}, "household", ())
    bus.deliver([event])
    off()
    off()  # twice: a no-op
    bus.deliver([event])
    assert seen == [1]


@pytest.mark.req("EF-INT-002", "EF-USR-021", *SEE)
def test_sse_a_child_key_reads_as_the_child_and_its_revocation_ends_the_stream(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kid = world.kid_id
    auth = local_auth(world.seed.engine, world.env.settings)
    created = auth.create_api_key(name="kidstream", username="kid", read_only=True)
    bearer = {"Authorization": f"Bearer {created.value}"}

    def revoke() -> None:
        assert auth.revoke_api_key(created.prefix)
        _insert(world, "ok", actor=kid)  # the revoked key must not get this one

    received = open_stream(
        world.env.app, world.env.clock, monkeypatch, headers=bearer,
        script=[lambda: _hidden_and_visible(world, kid), revoke],
    )  # fmt: skip
    assert received.status == 200 and received.complete
    # the key of a child never reads more than the child does
    assert _works_of(received.frames) == [world.seed.works[k] for k in ("ok", "adult_shelf")]
    world.clean("".join(received.frames))


@pytest.mark.req("EF-INT-002", "EF-USR-021")
def test_credential_is_live_for_a_session_a_key_and_a_profile(world: World) -> None:
    engine = world.seed.engine
    auth = local_auth(engine, world.env.settings)
    live = auth.create_api_key(name="live", username="kid", read_only=True)
    short = auth.create_api_key(name="short", username="kid", read_only=True, expires_days=1)
    principal = auth.authenticate_api_key(live.value)
    expiring = auth.authenticate_api_key(short.value)
    assert principal is not None and expiring is not None
    assert short.expires_at is not None
    before, after = short.expires_at - timedelta(seconds=1), short.expires_at + timedelta(seconds=1)
    assert credential_is_live(engine, principal, before)
    assert credential_is_live(engine, expiring, before)
    assert not credential_is_live(engine, expiring, after)  # past its expiry
    assert auth.revoke_api_key(live.prefix)
    assert not credential_is_live(engine, principal, before)
    # a credential kind nobody opens a stream with, and a profile that is gone
    assert not credential_is_live(
        engine, replace(principal, credential=Credential.LOCAL_CLI), before
    )
    assert not credential_is_live(
        engine, replace(expiring, user_id="01HZZZZZZZZZZZZZZZZZZZZZZZ"), before
    )
