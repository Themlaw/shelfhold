# ruff: noqa: F811  (the `world` fixture of the child suite is imported, then used as an argument)
"""Counter-review of #213 (M3 L12): red PoCs. Each asserts the behaviour the decision promises."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.events.stream import RESYNC_FRAME
from shelfhold.identity.domain.credentials import (
    HARDENED_ADMIN_POLICY,
    SessionTimes,
    session_expired,
)
from shelfhold.identity.public import Credential, Principal, credential_is_live
from shelfhold.infrastructure.event_sequence import invalidate_tokens
from shelfhold.infrastructure.schema import identity
from shelfhold.library.adapters.purge_inventory import erase_history
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.integration.test_l12_events_sse import (
    _admin_id,
    _advance,
    _feed,
    _id_of,
    _insert,
    _is_heartbeat,
    _started,
    _works_of,
)

UNIT = "01HZ0000000000000000000ABC"  # a Unit id (Crockford: no U)


@pytest.mark.req("EF-INT-002", "EF-USR-006")
def test_counter_poc_purge_of_a_hidden_unit_resyncs_the_child(world: World) -> None:
    """L12 §2: `resync_required` is never caused by a hidden event. A Unit purge (not a Work
    purge: no `invalidate_tokens`) erases the events that cite the Unit; when the child's last
    cursor (a heartbeat) sits on such a hidden row, its digest moves and her reconnection is
    `resync_required`. The adult's purge of something she never saw reaches her."""
    kid = world.kid_id
    feed = _started(world, kid)
    _insert(world, "ok", actor=kid)  # visible to her
    _insert(
        world, "above", actor=None, kind="unit.added",
        payload={"work_id": world.seed.works["above"], "unit_id": UNIT},
    )  # fmt: skip  # hidden from her
    _advance(world, 15)
    frames = feed.poll()
    assert _works_of(frames) == [world.seed.works["ok"]]  # the hidden row gave her nothing
    beat = _id_of(next(f for f in frames if _is_heartbeat(f)))
    assert _feed(world, kid, last_event_id=beat).start() == []  # control: a plain replay
    with world.seed.engine.begin() as connection:
        erased = erase_history(connection, {UNIT}, ())  # the Unit purge's erasure, nothing else
    assert erased[1] == 1
    assert _feed(world, kid, last_event_id=beat).start() == []  # red: RESYNC_FRAME


@pytest.mark.req("EF-INT-002", "EF-BIB-053")
def test_counter_poc_open_stream_survives_a_work_purge(world: World) -> None:
    """L12 §4: after a Work purge "the client reloads on `resync_required`". An open stream never
    says it: `poll` checks the epoch and the scope, not the floor. Its next frame carries a cursor
    above the new floor, so the page keeps the purged Work (and its title) for good."""
    admin = _admin_id(world)
    feed = _started(world, admin)
    with world.seed.engine.begin() as connection:
        invalidate_tokens(connection)  # what `erase_work_rows` does in the purge transaction
    _insert(world, "ok", actor=admin)
    frames = feed.poll()
    # (the first version also asserted here that a reconnection from the id of `frames[0]` replays
    # without resync: the frame is now `resync_required`, which carries no id; the reconnection
    # from a cursor of before the purge is `test_sse_cursor_from_before_a_work_purge_is_resync`)
    assert frames == [RESYNC_FRAME]


@pytest.mark.req("EF-INT-002", "EF-USR-021", "EF-USR-022")
@pytest.mark.parametrize("case", ["idle", "cap_after_switch"])
def test_counter_poc_stream_outlives_a_session_the_door_refuses(world: World, case: str) -> None:
    """L12 §5: "only the absolute expiry closes it". Under the hardened profile the door also
    refuses an admin session idle for 30 min, or older than 12 h when the profile was switched on
    after it opened. `credential_is_live` says yes to both: the stream of the most privileged
    reader (ops, every household entry) runs on up to 12 h, or up to 30 days."""
    engine = world.seed.engine
    admin = _admin_id(world)
    now = world.env.clock.now()
    times = {
        "idle": SessionTimes(now - timedelta(hours=1), now - timedelta(minutes=31),
                             now + timedelta(hours=11)),
        "cap_after_switch": SessionTimes(now - timedelta(hours=13), now - timedelta(minutes=1),
                                         now + timedelta(days=29)),
    }[case]  # fmt: skip
    assert session_expired(times, HARDENED_ADMIN_POLICY, now)  # the door says no
    with engine.begin() as connection:
        sid = (
            connection.execute(
                sa.select(identity.session.c.id).where(identity.session.c.user_id == admin)
            )
            .scalars()
            .first()
        )
        assert sid is not None
        connection.execute(
            sa.update(identity.session).where(identity.session.c.id == sid).values(
                created_at=times.created_at, last_seen_at=times.last_seen_at,
                absolute_expiry=times.absolute_expiry, revoked_at=None,
            )
        )  # fmt: skip
    principal = Principal(admin, "admin", "admin", Credential.SESSION, session_id=sid)
    assert not credential_is_live(engine, principal, now)  # red: the stream goes on
