"""The second, adverse reading of M5 L07 (EF-LEC-007, EF-LEC-014, ENF-SEC-010, EF-BIB-052).

Every test here is a hole found by reading the lot against its own claims: red on the branch
(`xfail(strict=True)`), it fails for the reason its message says, and it turns loud (XPASS) the
day the hole is closed. What the same reading found solid stays covered by the lot's own tests:
the profile is the session's on every door and an unknown field never chooses it; a Work hidden
from a child answers like one that is not there, whichever of the locator, the date or the
ticket is wrong first; `client_ts` is bounded by the server's clock or refused; the history and
the locator are closed shapes; the EPUB anchor never leaves for Kavita; one proposal is open per
profile and carrier; the migration, its partial index and the erasure of a profile are pinned by
`test_schema_migration_0102`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, reading
from tests.auth_env import CSRF_HEADER
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import count, kept, page, put
from tests.integration.test_schema_storage_export import add_destination, add_target
from tests.reading_env import NEW_HASH, reading_env


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    """The same world as the lot's own tests: the source cuts excerpts by the server's code."""
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def _read_elsewhere(world: World, content: str) -> None:
    """Send the Work where the member reads it: her kept copy is an excerpt from now on
    (D-P1), whatever the reader asks of it (`ContentAccess._full_allowed` turns false)."""
    item = world.export_item(world.seed.editions["ok"], content)
    with write_transaction(world.seed.engine) as connection:
        target = add_target(connection)
        destination = add_destination(connection, world.seed.shelves["family"], target)
        connection.execute(
            sa.text("INSERT OR IGNORE INTO secret_key (kid, state) VALUES ('k1', 'active')")
        )
        connection.execute(
            sa.insert(identity.destination_account).values(
                id=new_ulid(), user_id=user_id_of(world.seed.engine, "admin"),
                shelf_destination_id=destination, remote_user="remote-admin",
                secret_v=1, secret_kid="k1",  # noqa: S106 (a key id, not a secret)
                secret_nonce=os.urandom(12), secret_ct=os.urandom(32),
                token_fingerprint="f" * 12, verified_at=sa.func.current_timestamp(),
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=destination, export_item_id=item,
                locator_type="none", state="written",
            )
        )  # fmt: skip


@pytest.mark.req("EF-LEC-007", "EF-LEC-014", "ENF-SEC-010")
def test_the_choice_writes_nothing_where_only_an_excerpt_is_read(world: World) -> None:
    """The proposal was made while the kept copy could still be read in full; the Work has since
    been sent where the member reads it, so her copy is an excerpt now (D-P1). The reader still
    sees the proposal (`GET` reads), but settling it must write nothing: the lot's rule is that an
    excerpt leaves no anchor, no history and no proposal, and a choice is not a reading."""
    content, _unit = kept(world)
    assert put(world, world.admin, content, page(40, 50), device="tab", minutes=-30).is_success
    raised = put(world, world.admin, content, page(2, 50), device="phone", minutes=-10)
    proposal = raised.json()["conflict"]
    assert proposal is not None  # the divergence was asked while the copy was readable in full
    _read_elsewhere(world, content)
    refused = put(world, world.admin, content, page(3, 50), minutes=-5)
    assert refused.status_code == 409  # the door the lot closed on the writing route
    assert refused.json()["code"] == "excerpt_writes_no_position"
    before = count(world, reading.reading_position)
    settled = world.admin.post(
        f"/api/reader/conflicts/{proposal['id']}/resolve",
        json={"choice": "loser"},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert count(world, reading.reading_position) == before, (
        "the choice wrote an anchor on a carrier only an excerpt may be read of"
    )
    assert settled.status_code == 409, settled.text


@pytest.mark.req("EF-BIB-052", "EF-LEC-007")
def test_a_purged_work_takes_its_proposals_with_it(tmp_path: Path) -> None:
    """`PurgeService._erase_rows` is the step of `purge --now` that takes the rows of a Work out
    of the base, and it takes the `reading_position` anchors with it (EF-BIB-052: nothing of the
    Work stays behind). An open `reading_conflict` points at two of those anchors by id and at
    the carrier by id: it is a row of the same Work, and it must go with them — an open one
    whose carrier is gone can never be settled (its `resolve` is the 404 of an unknown carrier),
    and the ids it keeps outlive the purge."""
    from shelfhold.export.application.purge import PurgeService

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        unit = env.units("ok", 1)[0]
        item = env.file("ok", [unit], content_hash=NEW_HASH)
        with env.engine.connect() as connection:
            content = str(
                connection.execute(
                    sa.select(export.export_item.c.content_id).where(
                        export.export_item.c.id == item
                    )
                ).scalar_one()
            )
        far = env.position(reader, content, carrier_type="content", page=40, device_id="tab")
        near = env.position(reader, content, carrier_type="content", page=2, device_id="phone")
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(reading.reading_conflict).values(
                    id=new_ulid(), user_id=reader, carrier_type="content", carrier_id=content,
                    winner_position_id=near, loser_position_id=far,
                )
            )  # fmt: skip
        plan = type("Plan", (), {"unit_ids": (unit,), "items": (item,), "contents": (content,)})()
        with write_transaction(env.engine) as connection:
            PurgeService._erase_rows(connection, plan, allowed=None)
        with env.engine.connect() as connection:
            anchors = connection.execute(
                sa.select(sa.func.count()).select_from(reading.reading_position)
            ).scalar_one()
            proposals = connection.execute(
                sa.select(sa.func.count()).select_from(reading.reading_conflict)
            ).scalar_one()
        assert anchors == 0  # the anchors of the Work went with it
        assert proposals == 0, "an open proposal outlives the Work it was made on"
