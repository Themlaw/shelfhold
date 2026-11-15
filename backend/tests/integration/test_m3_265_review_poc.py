# ruff: noqa: F811  (the `world` fixture of the child suite is imported, then used as an argument)
"""Adversarial review of #265 (a child's arrived request of a hidden Work reads `not_found`; one
definition of the family shelf): proofs of concept, red on purpose (`xfail` strict until fixed:
the fix makes them fail, then the marker goes). See `docs/decisions/M3.md`, "#265 : contradicteur".
The production code is not touched here.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)

pytestmark = pytest.mark.req("EF-USR-006", "EF-USR-010", "ENF-SEC-010")

REQUESTS = "/api/v1/requests"
SAME_TEXT = "Le livre que j'ai demande"


def _request(world: World, state: str, work_id: str | None) -> str:
    request_id = new_ulid()
    with world.seed.engine.begin() as connection:
        connection.execute(
            sa.insert(identity.request).values(
                id=request_id, requester_user_id=world.kid_id, target_kind="free_text",
                target_value=SAME_TEXT, state=state, visibility="household", work_id=work_id,
            )
        )  # fmt: skip
    return request_id


# -- 1. "introuvable" (hidden) is not the same answer as "introuvable" (absent) ------------------


def test_265_poc_hidden_and_absent_are_one_answer_in_her_requests(world: World) -> None:
    """She asked twice for the same text. The admin closed one `not_found` (nothing exists); the
    other arrived as a Work rated 16 (above her ceiling of 10), so #265 shows it `not_found`. The
    two must read the same, field by field (bar the id and the dates). They do not: the real
    `not_found` keeps her own text as `title`, the masked one drops it (`title=None`). "An
    introuvable without a title" is exactly "it exists and it is hidden from you"."""
    absent = _request(world, "not_found", None)
    hidden = _request(world, "arrived", world.seed.works["above"])
    items = {i["id"]: i for i in world.kid.get(REQUESTS).json()["items"]}

    def shape(item: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k not in ("id", "created_at", "updated_at")}

    assert items[hidden]["state"] == "not_found"  # the fix of #265 is there
    assert shape(items[hidden]) == shape(items[absent])


# -- 2. the Apprise notice counts an arrival the request says is introuvable ---------------------


def test_265_poc_no_notice_for_an_arrival_she_reads_as_introuvable(settings: Settings) -> None:
    """Notifications x requests. A Work rated 6+ (under her ceiling of 6) arrives as a second
    Edition nobody rated: the Edition is out of her sight. #265 shows her request `not_found`
    (it checks the Edition) and hides `ops.request_arrived` from her activity (it now names the
    Edition). `NotificationRouter._request_arrived` puts only `work_id` in its item, so the
    projection checks the Work alone, and the notice says "1 request(s) arrived": the channel
    counts what her list says does not exist."""
    from shelfhold.catalog.public import visible_edition_ids, visible_work_ids
    from shelfhold.identity.public import reader_scope
    from tests.integration.test_schema_catalog import add_edition
    from tests.notify_env import CHILD_ID as KID
    from tests.notify_env import world as notify_world

    with notify_world(settings) as ctx:
        work_id = ctx.work("Tale of Two Editions", age=6)
        with write_transaction(ctx.engine) as connection:
            hidden_edition = add_edition(connection, work_id, lang="de")  # arrived, never rated
            request_id = new_ulid()
            connection.execute(
                sa.insert(identity.request).values(
                    id=request_id, requester_user_id=KID, target_kind="work_id",
                    target_value=work_id, state="arrived", visibility="private",
                    work_id=work_id, edition_id=hidden_edition,
                )
            )  # fmt: skip
        with ctx.engine.connect() as connection:
            scope = reader_scope(connection, KID)
            assert visible_work_ids(connection, scope, {work_id}) == {work_id}
            assert visible_edition_ids(connection, scope, {hidden_edition}) == set()
        ctx.profile(KID, notify_mode="immediate")
        ctx.publish(
            "ops.request_arrived", {"request_id": request_id}, actor=KID, visibility="private"
        )
        ctx.run()
        told = ctx.sink.text(KID)
        assert "arrived" not in told, f"the notice counts an introuvable arrival: {told!r}"
