"""#378 (M5 L09), contradicteur: the PoC of what the review found, red until it is fixed.

C1 — a KOReader write never proposes the divergence of L07. `ReaderPositions.write` calls
`_propose` after each write, so when a second device writes **behind** the first one, the reader
is asked once (D8). `Kosync.push` writes the device's xpointer and proposes nothing; since
`_device_rows` makes that row the reader's latest position, the further place of the web reader
is silently left behind: the next write of the reader (on its own device) never brings it back,
because « going back on one's own device » is not a divergence.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export
from shelfhold.reading.public import Kosync
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import HASH, cfi, conflicts, current, kept, put
from tests.integration.test_m5_l09_merge import exported_twin

DIGEST = "0123456789abcdef0123456789abcdef"
DEVICE = "kobo-device"  # the `x-auth-user` of the Kobo
XPOINTER = "/body/DocFragment[1]/body/div[1]/p[2]/text().3"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def _kosync(world: World, monkeypatch: pytest.MonkeyPatch, item: str) -> Kosync:
    """The real service on the application's engine and clock; the file is known by its digest.
    Only the format lookup is a seam (an `export_record` needs a whole destination): the file is
    an EPUB, which is what `_kind` would say from its record."""
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.document_alias).values(
                export_item_id=item, file_hash="sha256:" + "e" * 64,
                koreader_partial_md5="md5:" + DIGEST, content_hash=HASH,
            )
        )  # fmt: skip
    monkeypatch.setattr(Kosync, "_kind", lambda _self, _c, _item: "reflowable")
    return Kosync(world.seed.engine, lambda: None, world.env.clock.now)  # type: ignore[arg-type, return-value]


@pytest.mark.req("EF-LEC-014", "EF-LEC-007")
def test_koreader_behind_the_reader_is_proposed_not_silently_taken(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    content, _unit = kept(world, fmt="epub")
    twin = exported_twin(world, content)
    service = _kosync(world, monkeypatch, twin)
    # the web reader is at 70 % on its phone
    assert put(world, world.admin, content, cfi(0.70), device="phone", minutes=-10).is_success
    # later, KOReader opens the book on the Kobo at its start (a new device: 3 %)
    world.env.clock.advance(timedelta(minutes=5))
    stored = service.push(
        user_id=user_id_of(world.seed.engine, "admin"), token=DEVICE, document=DIGEST,
        progress=XPOINTER, percentage=0.03, device="Libra 2", device_id="KOBO1",
    )  # fmt: skip
    assert stored is True
    here = current(world.admin, content).json()
    assert here["position"]["locator"].get("xpointer") == XPOINTER  # the device's place is current
    # what L07 does for two readers: the further place of the other device is asked about, once
    assert here["conflict"] is not None, "70 % of the phone dropped without a proposal"
    assert conflicts(world) == 1
