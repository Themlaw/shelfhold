"""#320 (M4 L08b), the minor points: unheld pauses, the new ceiling, strict libraries."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.domain.bindings import UnpartitionedDestinationError
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export
from tests.fakes.komga import administrator, reader
from tests.family_env import family_env
from tests.fan_out_env import bind, service
from tests.integration.test_komga_access_audit import LIBRARY, admit, kit, library
from tests.komga_env import run


def pause_by_hand(env, shelf_id: str) -> None:  # type: ignore[no-untyped-def]
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == shelf_id)
            .values(paused_reason="child_exposure")
        )


def paused(env, shelf_id: str) -> str | None:  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        value: str | None = connection.execute(
            sa.select(export.shelf.c.paused_reason).where(export.shelf.c.id == shelf_id)
        ).scalar_one()
    return value


@pytest.mark.req("EF-EXP-047")
@pytest.mark.parametrize("bad", ["too_many", "unreadable_row"])
def test_an_unreadable_or_oversized_library_list_fails_the_audit(bad: str) -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.libraries = (
            [library(f"0LIB{i:05d}", f"/other/{i}") for i in range(501)]
            if bad == "too_many"
            else [library(), {"id": 7}]  # type: ignore[list-item]
        )
        report = admit(env, auditor, client, target)
        assert report.failed and not report.proved and report.reason == "audit_failed"


@pytest.mark.req("EF-EXP-047")
def test_an_unheld_pause_of_a_shelf_bound_to_this_server_alone_is_lifted() -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, auditor, client, target).proved
        bind(env, "Komga", target_id=target, link=False)
        pause_by_hand(env, env.shelf_id)  # a change of ceiling or of path map, no audit's hold
        run(auditor.audit(client, target))
        # the shelf is also bound to the first server of the environment: not alone, not lifted
        assert paused(env, env.shelf_id) == "child_exposure"


@pytest.mark.req("EF-EXP-047")
def test_the_ceiling_a_shelf_is_about_to_have_is_the_one_checked() -> None:
    with family_env(13) as env:
        fake, client, auditor, _events, target = kit(env)
        fake.users = [administrator(), reader("teen", [LIBRARY], age=13)]
        assert admit(env, auditor, client, target).proved
        bind(env, "Komga", target_id=target, link=False)
        with pytest.raises(UnpartitionedDestinationError):  # proved for 13, not for 16
            service(env).set_max_age(env.shelf_id, 16)
