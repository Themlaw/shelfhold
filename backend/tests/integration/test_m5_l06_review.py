"""#366, independent adverse review of M5 L06 (ADR-0055 D-P1, EF-LEC-014, I35 extended).

Each test is a proof of a hole found by the review, marked `xfail(strict=True)`: it fails today and
must pass once the hole is closed (then the mark goes).

- R1: the full reader is refused for a kept copy « read elsewhere » only while its export record is
  `written`; once the destination has scanned it (`scanned`, `resolved`, `adopted`: the states in
  which the file is most certainly readable there), the full book is served again.
- R2: the kept copy of a `content` (CBZ) is opened as « the first library copy of its Unit », not
  as a copy of that content: another content of the same Unit (another variant, the exported file)
  is served under the ticket and the manifest of the first one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, storage
from tests.content_env import zip_of
from tests.integration.test_l05a_content_route import World
from tests.integration.test_m5_l06_publication import (
    book,
    pages_of,
    publication,
    unit_of,
)
from tests.integration.test_m5_l06_publication import world as _world_fixture

world = _world_fixture  # the fixture of the lot: the real application, excerpts cut by the server


def _read_elsewhere(world: World, item: str, state: str) -> None:
    """The admin has a live, verified account on a destination holding `item` in `state`."""
    from tests.integration.test_schema_storage_export import add_destination, add_target

    with write_transaction(world.seed.engine) as connection:
        target = add_target(connection)
        destination = add_destination(connection, world.seed.shelves["family"], target)
        connection.execute(
            sa.text("INSERT OR IGNORE INTO secret_key (kid, state) VALUES ('k1', 'active')")
        )
        admin = connection.execute(
            sa.select(identity.user.c.id).where(identity.user.c.username == "admin")
        ).scalar_one()
        connection.execute(
            sa.insert(identity.destination_account).values(
                id=new_ulid(), user_id=admin, shelf_destination_id=destination,
                remote_user="remote-admin", secret_v=1, secret_kid="k1",  # noqa: S106
                secret_nonce=os.urandom(12), secret_ct=os.urandom(32),
                token_fingerprint="f" * 12, verified_at=sa.func.current_timestamp(),
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=destination,
                export_item_id=item,
                locator_type="none",
                state=state,
            )
        )


@pytest.mark.xfail(
    strict=True,
    reason="R1: `_full_allowed` counts only `written`; scanned/resolved/adopted re-open the book",
)
@pytest.mark.req("EF-LEC-014")
@pytest.mark.parametrize("state", ["scanned", "resolved", "adopted"])
def test_r1_poc_scanned_export_is_still_read_elsewhere(world: World, state: str) -> None:
    content = book(world, pages_of(30))
    unit = unit_of(world, content)
    item = world.export_item(world.seed.editions["ok"], content)
    world.src.item_content[item] = content
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip
    _read_elsewhere(world, item, state)  # Kavita saw the file: it is read there
    full = publication(world.admin, "content", content, scope="full")
    assert full.status_code == 409 and full.json()["code"] == "full_read_not_allowed"
    chosen = publication(world.admin, "unit", unit).json()
    assert (chosen["carrier_type"], chosen["scope"]) == ("export_item", "excerpt")


@pytest.mark.xfail(
    strict=True,
    reason="R2: a CBZ `content` is opened from the first library copy of its Unit, whatever it is",
)
@pytest.mark.req("EF-LEC-005", "EF-LEC-014")
def test_r2_poc_a_content_reads_only_a_copy_of_itself() -> None:
    """A Unit holds two library copies: its content's, and one registered as a copy of another
    content (another variant, say). A ticket on the first serves the file of the second, in full,
    with no look at its identity: under D-P1 that other file may be the exported one, read
    elsewhere, that no full ticket may open."""
    from shelfhold.infrastructure.schema import replica as replica_schema
    from shelfhold.library.public import ReadableCopies
    from tests.export_env import export_env

    replica = replica_schema.replica
    with export_env() as env:
        series = env.main_series()
        unit = env.add_chapter(series, "77")
        with env.engine.connect() as connection:
            content_id = connection.execute(
                sa.select(storage.content.c.id).where(storage.content.c.unit_id == unit)
            ).scalar_one()
            own = connection.execute(
                sa.select(replica).where(replica.c.unit_id == unit, replica.c.location == "library")
            ).one()
        # another content's copy, beside it in /library, registered before it (a smaller id)
        root = _root_of(env, own.physical_root_id)
        foreign = Path(root) / Path(own.rel_path).parent / "other-variant.cbz"
        foreign.write_bytes(zip_of({"x-001.png": b"\x89PNG\r\n\x1a\nFOREIGN"}))
        values = {k: v for k, v in own._mapping.items() if k != "id"}
        values.update(
            id="00000000000000000000000000",
            rel_path=str(Path(own.rel_path).parent / "other-variant.cbz"),
            content_hash="sha256:" + "f" * 64,
            file_hash="sha256:" + "e" * 64,
            size=foreign.stat().st_size,
            dev=None,
            ino=None,
        )
        with env.engine.begin() as connection:
            connection.execute(sa.insert(replica).values(**values))
        archive = ReadableCopies(env.engine, lambda: env.lib.fsops).open("content", content_id)
        try:
            assert "x-001.png" not in archive.names()  # not the other content's file
        finally:
            archive.close()


def _root_of(env: object, root_id: str) -> str:
    from shelfhold.infrastructure.schema import platform

    with env.engine.connect() as connection:  # type: ignore[attr-defined]
        return str(
            connection.execute(
                sa.select(platform.physical_root.c.path_entry).where(
                    platform.physical_root.c.root_id == root_id
                )
            ).scalar_one()
        )
