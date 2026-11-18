"""Second adversarial review of L06 (feat/m5-l06-publication, after #366): what still does not hold.

Each case below is a hole found on the head of the branch; it is red today and turns green with
the fix it names (`xfail(strict=True)`; `--runxfail` shows it red, the mark is lifted once fixed).
The archives come from a source the test controls, with the bound of an excerpt cut by the real
functions of the server (L05a), and every answer is read through the real route.

S1 — a full read of a Work the profile reads where it was sent. `ContentAccess._full_allowed`
counts only the records of state `written`, but the states that say the destination **holds** the
file are `written`, `scanned`, `resolved` and `adopted` everywhere else in the domain (the `LIVE`
of the blocks, the `_HOLDING` of the Kavita progress, the `RECORD_VERIFIED` of the safeguard
inventory). A record the polling has *seen* the destination hold (`resolved`, or a file adopted
from it), with the profile's live and verified account on that destination, is invisible to the
rule: the manifest answers `scope=full` by default, `?scope=full` is honoured, and `/content`
serves the whole book under the full ticket — the second complete reading venue D-P1 forbids.

S2 — the manifest of a Unit serves the file of another Unit. `PublicationService.choose` takes the
first `export_item` that *covers* the Unit without checking that its `content_id` is a content of
that Unit (or even of its Edition): nothing ties an item to what it points at, the very premise
the `pull` binding learned to check (`_kept_copies`). An item made to cover a Unit while naming the
content of another Unit of another Edition serves that other book under this Unit's name: its
title, its links, its bytes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, storage
from shelfhold.reading.domain.excerpt import page_excerpt
from tests.content_env import PNG, FakeArchive, FakeArchives, add_carrier
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_schema_catalog import add_unit
from tests.integration.test_schema_storage_export import add_destination, add_target


class ServerCutArchives(FakeArchives):
    """The source of the test, for image carriers: an excerpt is the closed list the real
    `page_excerpt` cuts, as the port of `ContentAccess` computes it (a CBZ only; another format
    has no readable copy)."""

    def open(self, carrier_type: str, carrier_id: str, scope: str = "full") -> FakeArchive:
        from shelfhold.reading.application.content_access import (
            ExcerptUnavailableError,
            NoReadableCopyError,
        )

        content_id = (
            self.item_content.get(carrier_id) if carrier_type == "export_item" else carrier_id
        )
        if content_id is None or content_id not in self.by_content:
            raise NoReadableCopyError
        entries = self.by_content[content_id]
        if scope == "excerpt":
            pages = page_excerpt([n for n in entries if n.endswith(".png")])
            if pages is None:
                raise ExcerptUnavailableError
            entries = {n: b for n, b in entries.items() if n in pages}
        return FakeArchive(entries, self.asked)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def book(world: World, entries: dict[str, bytes], *, edition: str = "ok") -> str:
    """A kept copy for a Unit of the Edition, served from `entries` and cut by the server."""
    content_id = add_carrier(world.seed.engine, world.seed.editions[edition], format="cbz")
    world.src.by_content[content_id] = dict(entries)
    return content_id


def bare_unit(world: World, edition: str = "ok", number: str = "99") -> str:
    """A Unit of the Edition with no content of its own: nothing of it may be read."""
    with write_transaction(world.seed.engine) as connection:
        return add_unit(connection, world.seed.editions[edition], number)


def unit_of(world: World, content_id: str) -> str:
    with world.seed.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == content_id)
            ).scalar_one()
        )


def pages_of(count: int) -> dict[str, bytes]:
    return {f"p{n}.png": PNG + str(n).encode() for n in range(1, count + 1)}


def publication(client: Any, carrier_type: str, carrier_id: str, **params: Any) -> Any:
    return client.get(f"/api/reader/{carrier_type}/{carrier_id}/publication", params=params)


def covered_by(world: World, item: str, unit: str) -> None:
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip


def account_on(world: World, destination: str, username: str = "admin") -> None:
    """A live, verified account of the profile on the destination (the word of D-P1: the file is
    read where it was sent)."""
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.text("INSERT OR IGNORE INTO secret_key (kid, state) VALUES ('k1', 'active')")
        )
        user = connection.execute(
            sa.select(identity.user.c.id).where(identity.user.c.username == username)
        ).scalar_one()
        connection.execute(
            sa.insert(identity.destination_account).values(
                id=new_ulid(), user_id=user, shelf_destination_id=destination,
                remote_user="remote", secret_v=1, secret_kid="k1",  # noqa: S106
                secret_nonce=os.urandom(12), secret_ct=os.urandom(32),
                token_fingerprint="f" * 12, verified_at=sa.func.current_timestamp(),
            )
        )  # fmt: skip


# -- S1: a record the destination was seen to hold is an export the profile reads -----------------


@pytest.mark.xfail(
    strict=True,
    reason="D-P1: a record the destination was seen to hold (scanned, resolved, adopted) is an "
    "export the profile reads there, not a full read here",
)
@pytest.mark.req("EF-LEC-014", "EF-EXP-058")
@pytest.mark.parametrize("state", ["resolved", "scanned", "adopted"])
def test_a_record_the_destination_was_seen_to_hold_is_read_there_not_in_full(
    world: World, state: str
) -> None:
    """The exported file was confirmed on the destination — the polling saw the scan hold it
    (`resolved`), a rescan was seen (`scanned`), or the library was adopted from it (`adopted`) —
    and the profile has a live, verified account there. D-P1 opens no second complete reading
    venue: the manifest answers a sample, `scope=full` is refused, and the server chooses the
    exported file for the Unit. A record merely `written` already answers exactly that."""
    content = book(world, pages_of(30))
    unit = unit_of(world, content)
    item = world.export_item(world.seed.editions["ok"], content)
    world.src.item_content[item] = content
    covered_by(world, item, unit)
    with write_transaction(world.seed.engine) as connection:
        target = add_target(connection)
        destination = add_destination(connection, world.seed.shelves["family"], target)
    account_on(world, destination)
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=destination, export_item_id=item,
                locator_type="none", state=state,
            )
        )  # fmt: skip

    named = publication(world.admin, "content", content)  # the default follows the rule
    assert named.status_code == 200, named.text
    assert named.json()["scope"] == "excerpt"
    sample = named.json()["pages"]
    assert len(sample) == 6  # 30 pages: a fifth is 6, the closed list of the server
    ticket = sample[0]["href"].split("/content/", 1)[1].split("/", 1)[0]
    assert world.admin.get(f"/content/{ticket}/p6.png").status_code == 200
    assert world.admin.get(f"/content/{ticket}/p7.png").status_code == 404  # beyond the sample

    refused = publication(world.admin, "content", content, scope="full")
    assert refused.status_code == 409
    assert refused.json()["code"] == "full_read_not_allowed"
    assert "/content/" not in refused.text

    by_unit = publication(world.admin, "unit", unit).json()  # the server chooses the exported file
    assert (by_unit["carrier_type"], by_unit["carrier_id"]) == ("export_item", item)
    assert by_unit["scope"] == "excerpt"


# -- S2: the manifest of a Unit is made of a content of that Unit ----------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="the manifest of a Unit serves a content of that Unit: an export_item that covers it "
    "while naming another Unit's content is no file of it",
)
@pytest.mark.req("EF-LEC-014", "EF-EXP-058")
def test_a_unit_is_read_from_no_other_units_file(world: World) -> None:
    """Nothing ties an `export_item` to the Unit its coverage names (the `pull` binding checks
    every link for that reason). One that covers a Unit while naming the content of another Unit of
    another Edition is not a file of it: the Unit has no readable copy of its own, and the answer
    is the typed refusal — never the other book's title, links and bytes under this Unit's name."""
    mine = bare_unit(world, "ok", "99")  # a Unit with nothing of its own to read
    other = book(world, pages_of(30), edition="mixed_rated")  # another Edition's own book
    item = world.export_item(world.seed.editions["ok"], other)
    world.src.item_content[item] = other
    covered_by(world, item, mine)

    got = publication(world.admin, "unit", mine)
    assert got.status_code == 409, f"another Unit's file was read under this Unit: {got.text[:200]}"
    assert got.json()["code"] == "no_readable_copy"
    assert "/content/" not in got.text
    assert "Mixed" not in got.text  # not even the other book's title
