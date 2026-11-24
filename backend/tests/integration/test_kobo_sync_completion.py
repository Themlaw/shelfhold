"""The sync point of a Kobo device is written once per device and updated in place by the next
completion; a token revoked between the door and the sync is refused (EF-INT-012)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.schema import identity
from shelfhold.reading.public import KoboSync, TokenGoneError
from tests.integration.test_l17a_kobo_sync import SYNC, World, add_work, world

pytestmark = pytest.mark.req("EF-INT-012")

__all__ = ["world"]  # the fixture of the sync tests


def test_the_point_is_created_then_updated_in_place(world: World) -> None:
    mum = world.mum
    assert mum.get(SYNC).status_code == 200
    first = world.sync_points()
    assert len(first) == 1  # created by the first completion

    add_work(world, "newcomer", age=6, shelf="adult")
    assert mum.get(SYNC).status_code == 200
    second = world.sync_points()
    assert len(second) == 1 and second[0].id == first[0].id  # updated, never a second row


def test_a_token_revoked_after_the_door_is_refused(world: World) -> None:
    with world.seed.engine.connect() as connection:
        token_id = str(
            connection.execute(
                sa.select(identity.device_token.c.id).where(
                    identity.device_token.c.public_id == world.mum.public_id
                )
            ).scalar_one()
        )
    with world.seed.engine.begin() as connection:
        connection.execute(sa.delete(identity.device_token))
    principal = Principal(
        user_id=world.user_id("mum"),
        username="mum",
        role="member",
        credential=Credential.DEVICE_TOKEN,
        key_id=token_id,
    )
    service = KoboSync(world.seed.engine, lambda: datetime.now(UTC))

    with pytest.raises(TokenGoneError):
        service.sync(principal=principal, token_id=token_id, base="/kobo/x", continuation=None)
    assert world.sync_points() == []
