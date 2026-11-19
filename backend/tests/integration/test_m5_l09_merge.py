"""M5 L09 (EF-LEC-014, EF-LEC-007): the reader of the web and KOReader on one EPUB, by the rule of
L07.

The reader writes a `cfi` on the kept copy; a device writes an xpointer on the exported file of the
same content. Both are the positions of one reader on one book: the latest in effective time is
where she is, and a divergence is proposed once. What a device wrote comes back to the reader as
it was written (opaque), on its own file: it is never rewritten as a `cfi`, and never copied onto
the kept copy (I30).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, reading
from tests.auth_env import CSRF_HEADER
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import HASH, cfi, conflicts, current, kept, put

XPOINTER = "/body/DocFragment[3]/body/div[2]/p[12]/text().5"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def exported_twin(world: World, content: str) -> str:
    """The file the export placed for the same content: an `export_item` that points at it."""
    item = new_ulid()
    with write_transaction(world.seed.engine) as connection:
        edition = (
            connection.execute(
                sa.text(
                    "SELECT edition_id FROM unit WHERE id = (SELECT unit_id FROM content "
                    "WHERE id = :c)"
                ),
                {"c": content},
            ).scalar_one_or_none()
            or world.seed.editions["ok"]
        )
        connection.execute(
            sa.insert(export.export_item).values(
                id=item, edition_id=edition, kind="volume", content_id=content
            )
        )  # fmt: skip
    return item


def device_position(
    world: World, item: str, *, progression: float, minutes: float, device: str = "kobo",
    xpointer: str = XPOINTER, hash_: str = HASH, user: str = "admin",
) -> str:  # fmt: skip
    """What KOReader's PUT stored (`Kosync.push`): a `device` position with an xpointer."""
    when = world.env.clock.now() + timedelta(minutes=minutes)
    row = new_ulid()
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(reading.reading_position).values(
                id=row, user_id=user_id_of(world.seed.engine, user), carrier_type="export_item",
                carrier_id=item, content_hash=hash_,
                locator={"xpointer": xpointer, "spine": 2, "device": "Libra 2", "w": 1, "f": True,
                         "who": "d:" + device},
                percent=progression, client_ts=when, server_ts=when, device_id=device,
                source="device", covered_units=[],
            )
        )  # fmt: skip
    return row


def resolve(world: World, conflict_id: str, choice: str, device: str = "phone") -> Any:
    return world.admin.post(
        f"/api/reader/conflicts/{conflict_id}/resolve",
        json={"choice": choice, "device_id": device},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
def test_reader_and_koreader_positions_merge_on_same_epub(world: World) -> None:
    content, _unit = kept(world, fmt="epub")
    twin = exported_twin(world, content)
    # the reader of the web is at 20 %, and a minute later KOReader says 60 %, on its own file
    assert put(world, world.admin, content, cfi(0.20), device="phone", minutes=-10).is_success
    kobo = device_position(world, twin, progression=0.60, minutes=-5)
    here = current(world.admin, content).json()
    # the latest is where she is: the device's place, as it wrote it
    assert here["position"]["id"] == kobo
    assert here["position"]["locator"]["xpointer"] == XPOINTER  # opaque, not rewritten
    assert here["position"]["carrier_id"] == twin  # on its own file, never copied onto the copy
    # the reader goes on at 22 %: she is behind the device by more than D8's threshold -> asked once
    back = put(world, world.admin, content, cfi(0.22), device="phone", minutes=0)
    proposal = back.json()["conflict"]
    assert proposal is not None
    assert proposal["winner"]["locator"]["totalProgression"] == 0.22
    assert (
        proposal["loser"]["locator"]["xpointer"] == XPOINTER
    )  # « reprendre là où KOReader en est »
    assert (
        put(world, world.admin, content, cfi(0.23), device="phone", minutes=1).json()["conflict"][
            "id"
        ]
        == proposal["id"]
    )  # once
    assert conflicts(world) == 1
    # she resumes from the device's place: that is the latest position, an xpointer on the device's
    # file
    done = resolve(world, proposal["id"], "loser")
    assert done.status_code == 200, done.text
    assert done.json()["conflict"] is None
    assert done.json()["position"]["locator"]["xpointer"] == XPOINTER
    with world.seed.engine.connect() as connection:
        copies = connection.execute(
            sa.select(sa.func.count())
            .select_from(reading.reading_position)
            .where(
                reading.reading_position.c.carrier_id == content,
                sa.func.json_extract(reading.reading_position.c.locator, "$.xpointer").is_not(None),
            )
        ).scalar_one()
    assert copies == 0  # I30: the place of the exported file is never pasted on the kept copy


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
def test_koreader_and_reader_same_device_going_back_is_no_conflict(world: World) -> None:
    content, _unit = kept(world, fmt="epub")
    twin = exported_twin(world, content)
    device_position(world, twin, progression=0.60, minutes=-5, device="same")
    put(world, world.admin, content, cfi(0.10), device="same", minutes=0)
    assert current(world.admin, content).json()["conflict"] is None


@pytest.mark.req("EF-LEC-014", "ENF-SEC-010")
def test_koreader_position_of_another_content_or_profile_is_not_merged(world: World) -> None:
    content, _unit = kept(world, fmt="epub")
    twin = exported_twin(world, content)
    other = kept(world, fmt="epub", edition="twelve")[0]
    stranger_item = exported_twin(world, other)
    put(world, world.admin, content, cfi(0.10), device="phone", minutes=0)
    device_position(world, stranger_item, progression=0.90, minutes=-5)  # another book
    device_position(world, twin, progression=0.90, minutes=-5, hash_="sha256:" + "d" * 64)
    # the same content, but a file that is not the one the reader has (another hash): not merged
    here = current(world.admin, content).json()
    assert here["conflict"] is None and here["position"]["locator"]["type"] == "cfi"
    # the position of another profile on the very same file is never read here
    device_position(world, twin, progression=0.90, minutes=-5, user="mum")
    again = current(world.admin, content).json()
    assert again["conflict"] is None and again["position"]["locator"]["type"] == "cfi"
