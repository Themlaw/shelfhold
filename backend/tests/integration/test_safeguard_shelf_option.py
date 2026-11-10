"""Turning on the erasure of the upstream download (EF-SRC-009, B6): off by default, a reserved
action (never a key), audited, and the only writer of the option."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.export.application.shelves import ShelfService
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    Credential,
    CredentialNotAllowed,
    Principal,
    authorize_reserved,
)
from shelfhold.infrastructure.schema import export, identity
from shelfhold.interfaces.local_cli.ops import OPS_METHODS
from tests.library_env import lib_env
from tests.safeguard_env import World

pytestmark = pytest.mark.req("EF-SRC-009", "EF-USR-024")
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def principal(credential: Credential) -> Principal:
    return Principal(user_id="1000", username="uid:1000", role="admin", credential=credential)


def test_purge_upstream_is_a_reserved_action_a_key_cannot_use() -> None:
    action = RESERVED_ACTIONS["shelf.purge_upstream"]
    assert isinstance(
        authorize_reserved(principal(Credential.API_KEY), action, now=NOW), CredentialNotAllowed
    )
    assert isinstance(
        authorize_reserved(principal(Credential.DEVICE_TOKEN), action, now=NOW),
        CredentialNotAllowed,
    )
    assert isinstance(authorize_reserved(principal(Credential.LOCAL_CLI), action, now=NOW), Allowed)
    assert OPS_METHODS["ops.shelf_purge_upstream"][1] == "shelf.purge_upstream"


def test_option_is_off_by_default_and_has_one_audited_writer() -> None:
    with lib_env() as env:
        world = World(env, option=False)
        service = ShelfService(env.engine)
        assert service.shelf_id_of("S") == world.shelf_id
        with env.engine.connect() as c:
            assert (
                c.execute(sa.select(export.shelf.c.purge_upstream_after_export)).scalar_one()
                is False
            )
        service.set_purge_upstream(world.shelf_id, True)
        with env.engine.connect() as c:
            assert (
                c.execute(sa.select(export.shelf.c.purge_upstream_after_export)).scalar_one()
                is True
            )
            actions = c.execute(sa.select(identity.audit.c.action)).scalars().all()
        assert "shelf.set_purge_upstream" in actions
        service.set_purge_upstream(world.shelf_id, False)
        with pytest.raises(LookupError):
            service.set_purge_upstream("01NOSUCHSHELF000000000000", True)
        with pytest.raises(LookupError):
            service.shelf_id_of("nope")
