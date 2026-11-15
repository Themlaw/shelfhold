"""Adversarial review of the M3 closure (relecture adverse, transverse leaks): proofs of concept,
red on purpose (`xfail` strict until fixed: the fix makes them fail, then the marker goes).

Each test below combines two surfaces that the per-lot reviews looked at one by one. They are
expected to FAIL on `docs/m3-closure` as it stands; each one turns green when its objection is
fixed (see `docs/decisions/M3.md`). The production code is not touched here.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.requests import Estimate, RequestService
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from tests.auth_env import ManualClock, db_rows
from tests.federated_env import instance
from tests.integration.test_requests_http import (
    REQUESTS,
    admin_client,
    alpha,
    ask_as_child,
    family,
)
from tests.request_env import FakePorts, bind_work

pytestmark = pytest.mark.req("EF-USR-006", "EF-USR-010", "EF-UX-010", "ENF-SEC-010")

WORKS = "/api/v1/works"
ACTIVITY = "/api/v1/activity"


@pytest.fixture(autouse=True)
def ports(monkeypatch: pytest.MonkeyPatch) -> FakePorts:
    fakes = FakePorts(kinds={"alpha": "manga"}, estimate_of=Estimate(units=3, size_bytes=10))
    monkeypatch.setattr("shelfhold.ops.requests.build_ports", lambda _comp: fakes)
    return fakes


# -- 1. a request says "available" for a Work the child may not see ------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="M3 closure review M1: a request is `available` for a Work hidden from its requester",
)
def test_closure_poc_a_request_never_says_available_for_a_work_hidden_from_her(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ports: FakePorts
) -> None:
    """Requests (her own) x catalogue (the one filter of L09) x activity and SSE.

    A child (ceiling 6) asks for a series through the family search. The admin approves it with
    an age of 16 (`request.approve`: the age is the admin's to choose, nothing caps it at the
    child's ceiling). The series arrives: `arrived` rates the Work 16, so the Work is out of her
    sight on every catalogue surface (`/works`, search, `/me/state`). But her request turns
    `arrived` and reads `family_state: "available"` ("Disponible dans Kavita", with the link
    when a Kavita target exists), and the activity event `ops.request_arrived` (payload
    `{request_id}` only: `project_event` has no Work to check) tells her the same. The two
    surfaces together say "this book is in the library, and it is hidden from you": a hidden Work
    is no longer exactly an absent one."""
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        admin = admin_client(api)
        done = api.post(f"{REQUESTS}/{rid}/approve", client=admin, json={"age": 16})
        assert done.status_code == 200, done.text
        assert ports.follows  # the series is followed: the arrival comes with its first Unit

        engine = create_db_engine(api.settings.database_path)
        try:
            with write_transaction(engine) as connection:
                work_id = bind_work(connection, "alpha", "a1")
            clock = ManualClock()
            arrivals = RequestService(engine, clock, ports, bus=EventBus(clock))
            assert arrivals.arrived(work_id) == 1
        finally:
            engine.dispose()
        assert db_rows(api.settings, "SELECT age_rating FROM work WHERE id = ?", work_id) == [(16,)]
        # the catalogue: the Work is hidden from her (16 > 6), exactly like an absent one
        listed = kid.get(WORKS).json()
        assert work_id not in [w["work_id"] for w in listed["items"]]

        mine: dict[str, Any] = kid.get(f"{REQUESTS}/{rid}").json()
        feed = kid.get(ACTIVITY).json()
        available = [i for i in feed if i.get("family_state") == "available"]
        leaks = [("request", mine)] if mine["family_state"] == "available" else []
        leaks += [("activity", item) for item in available]
        assert not leaks, f"a Work hidden from her is said to be available: {leaks}"


# -- 2. a digest counts a chapter of an Edition the child may not see ----------------------------


@pytest.mark.xfail(
    strict=True,
    reason="M3 closure review m1: the digest counts a chapter of an Edition hidden from her",
)
def test_closure_poc_a_digest_never_counts_a_chapter_hidden_from_her(settings: Settings) -> None:
    """Notifications (Apprise digest) x activity and the catalogue: a counter says what the list
    hides.

    `unit.stored` names the Unit, its Edition and its Work. The activity page and the SSE stream
    project the whole payload (`project_event`: the Unit and the Edition must be visible too).
    `NotificationRouter._chapters` keeps only `work_id` and the series of the event in its item
    (`integrations/notify/service.py`, `_activity`), so the projection checks the Work alone. A
    child who follows a Work she may see is told "1 new chapter(s)" for a chapter of a second
    Edition of it that is not rated yet ("À classer": out of every shelf and out of her sight):
    the activity shows her nothing, her library shows nothing new, the digest counts it."""
    from shelfhold.events.activity import Viewer
    from shelfhold.events.projection import project_event
    from shelfhold.identity.public import reader_scope
    from shelfhold.infrastructure.schema import platform
    from shelfhold.ops.activity_feed import activity_event_of
    from tests.integration.test_schema_catalog import add_edition, add_unit
    from tests.notify_env import CHILD_ID as KID
    from tests.notify_env import world

    with world(settings) as ctx:
        work_id = ctx.work("Tale of Two Editions", age=6)  # rated 6+, under her ceiling (6)
        with write_transaction(ctx.engine) as connection:
            hidden_edition = add_edition(connection, work_id, lang="de")  # arrived, never rated
            unit_id = add_unit(connection, hidden_edition)
        ctx.follow(KID, work_id)
        ctx.profile(KID, notify_mode="immediate")
        published = ctx.publish(
            "unit.stored",
            {"unit_id": unit_id, "edition_id": hidden_edition, "work_id": work_id},
        )
        with ctx.engine.connect() as connection:
            row = connection.execute(
                sa.select(platform.event).where(platform.event.c.id == published.id)
            ).one()
            scope = reader_scope(connection, KID)
            shown = project_event(
                connection, Viewer(KID, "child"), scope, activity_event_of(connection, row)
            )
        assert shown is None  # the activity and the SSE stream hide this chapter from her
        ctx.run()
        told = ctx.sink.text(KID)
        assert "chapter" not in told, f"the digest counts a chapter hidden from her: {told!r}"
