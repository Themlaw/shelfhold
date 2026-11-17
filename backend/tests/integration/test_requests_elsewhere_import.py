"""M4 L30 (EF-USR-019): the real import folder closes the request that waited for the file.

A file dropped in the folder is filed by the production stores; the scan then matches it to the
open "to buy" request by the folded title, and the requester's request has `arrived`. A request
that is not open, or of another title, is left alone."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity
from tests.import_env import CLEAN_EPUB
from tests.integration.test_import_wiring import drop, folder_for
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-USR-019", "EF-SRC-012")


def _ask(engine: sa.Engine, user_id: str, text: str, state: str) -> str:
    request_id = new_ulid()
    with write_transaction(engine) as connection:
        connection.execute(
            sa.insert(identity.request).values(
                id=request_id,
                requester_user_id=user_id,
                target_kind="free_text",
                target_value=text,
                state=state,
                visibility="private",
            )
        )
    return request_id


def test_a_file_dropped_in_the_folder_closes_the_request_put_aside(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.composition import Composition
    from shelfhold.ops.import_folder import ImportOps

    folder = folder_for(tmp_path)
    with seeded(settings, tmp_path / "inst") as env:
        engine = env.inst.engine
        user_id = new_ulid()
        with write_transaction(engine) as connection:
            connection.execute(
                sa.insert(identity.user).values(id=user_id, username="reader", role="member")
            )
        waiting = _ask(engine, user_id, "Un Livre Acheté", "to_buy")
        pending = _ask(engine, user_id, "Un Livre Acheté Aussi", "pending")
        other = _ask(engine, user_id, "Autre Titre", "borrow_elsewhere")
        comp = Composition(
            settings.model_copy(update={"import_dir": folder}),
            engine, clock=SystemClock(), network=env.inst.network,
        )  # fmt: skip
        drop(folder, "un livre achete.epub", CLEAN_EPUB)  # the name has no accent, no capital
        report = ImportOps(comp).scan()
        assert [o["state"] for o in report["outcomes"]] == ["stored"]
        with engine.connect() as connection:
            states = dict(
                connection.execute(sa.select(identity.request.c.id, identity.request.c.state)).all()
            )
            work = connection.execute(
                sa.select(identity.request.c.work_id).where(identity.request.c.id == waiting)
            ).scalar_one()
        assert states == {waiting: "arrived", pending: "pending", other: "borrow_elsewhere"}
        assert work is not None
