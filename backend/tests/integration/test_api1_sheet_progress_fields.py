"""API-1 (EF-UX-006, EF-LEC-008, ENF-SEC-010): what the sheet, the home and the reader already know
and the screens could not read.

* `GET /editions/{id}/sheet` (the admin's): the kind, the authors, the years, the summary, the
  number of Units, the name of the shelf, the date and the source of the addition, and the
  administrator's own progress. Everything optional is `null` (or empty) when nobody said it.
* `GET /api/reader/editions/{id}/progress`: the profile's own progress on one Edition, for
  « Reprendre au chapitre N », with the filter of every surface (a hidden Edition is a 404).
* `GET /api/reader/continue`: each entry says where she is, on which device and when.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog
from tests.integration.test_l05a_content_route import World
from tests.integration.test_l43_series_sheet_api import _seed_follow
from tests.integration.test_m5_l06_publication import unit_of
from tests.integration.test_m5_l07_positions import kept, page, put
from tests.integration.test_m5_l14_continue_reading import (  # noqa: F401
    exported,
    items,
    pulled,
    read_there,
    world,
)

pytestmark = pytest.mark.req("EF-UX-006", "EF-LEC-008", "ENF-SEC-010")

MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"
FETCHED = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


def sheet(world: World, key: str = "ok") -> dict[str, Any]:  # noqa: F811
    got = world.admin.get(f"/api/v1/editions/{world.seed.editions[key]}/sheet")
    assert got.status_code == 200, got.text
    return dict(got.json())


def progress(client: Any, edition: str) -> Any:
    return client.get(f"/api/reader/editions/{edition}/progress")


def describe(world: World) -> None:  # noqa: F811
    work = world.seed.works["ok"]
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == work).values(
                description="An orchard, a river.", first_published="1870", date_precision="year"
            )
        )  # fmt: skip
        connection.execute(
            sa.update(catalog.edition).where(
                catalog.edition.c.id == world.seed.editions["ok"]
            ).values(published_on="1872", date_precision="year", author_death_year=1905)
        )  # fmt: skip
        connection.execute(
            sa.insert(catalog.metadata_field).values(
                entity_type="work", entity_id=work, field="contributors", lang="und",
                value=json.dumps(["Jules Verne", {"name": "Alphonse de Neuville"}]),
                provider="source-a", provenance="provider", fetched_at=FETCHED,
            )
        )  # fmt: skip


# -- the sheet ------------------------------------------------------------------------------------


def test_the_sheet_says_what_is_known_of_the_work(world: World) -> None:  # noqa: F811
    describe(world)
    _seed_follow(world.seed, world.seed.editions["ok"])
    kept(world)
    got = sheet(world)
    assert got["kind"] == "manga" or got["kind"]  # the Work's type, whatever the seed picked
    assert got["authors"] == ["Jules Verne", "Alphonse de Neuville"]
    assert got["first_published"] == "1870"
    assert got["published_on"] == "1872"
    assert got["author_death_year"] == 1905
    assert got["summary"] == "An orchard, a river."
    assert got["unit_count"] == 2  # the file of the world's setup and the one just kept
    assert got["shelf_id"] == world.seed.shelves["adult"] and got["shelf_name"] == "Adults"
    assert got["added_at"].startswith("20")
    assert got["added_source"] == "src-alpha"  # the first source that was bound


def test_the_sheet_invents_nothing(world: World) -> None:  # noqa: F811
    got = sheet(world, "adult_shelf")
    assert got["authors"] == []
    assert got["first_published"] is None and got["published_on"] is None
    assert got["author_death_year"] is None and got["summary"] is None
    assert got["added_source"] is None and got["progress"] is None
    assert got["unit_count"] == 0
    unshelved = sheet(world, "mixed_new")
    assert unshelved["shelf_id"] is None and unshelved["shelf_name"] is None


def test_the_sheet_carries_the_administrators_own_progress(world: World) -> None:  # noqa: F811
    assert sheet(world)["progress"] is None
    content, unit = kept(world)
    put(world, world.admin, content, page(15, 30), minutes=-2, device="tablet")
    got = sheet(world)["progress"]
    assert got["unit_id"] == unit and got["state"] == "in_progress"
    assert got["number"].isdigit() and got["device"] == "tablet"
    assert 0 < got["percent"] < 1 and got["last_read_at"]


def test_the_sheet_stays_the_admins_and_gives_a_child_nothing_of_it(world: World) -> None:  # noqa: F811
    describe(world)
    path = f"/api/v1/editions/{world.seed.editions['ok']}/sheet"
    for client in (world.kid, world.mum):
        got = client.get(path)
        assert got.status_code == 403
        for word in ("Jules Verne", "orchard", "Adults", "authors", "unit_count"):
            assert word not in got.text


# -- the progress of one Edition ------------------------------------------------------------


def test_the_progress_of_an_edition_is_none_before_any_reading(world: World) -> None:  # noqa: F811
    got = progress(world.admin, world.seed.editions["ok"])
    assert got.status_code == 200 and got.json() == {
        "edition_id": world.seed.editions["ok"],
        "progress": None,
    }


def test_the_progress_of_an_edition_names_the_last_unit_read(world: World) -> None:  # noqa: F811
    edition = world.seed.editions["ok"]
    first = unit_of(world, world.open_item)
    second, second_unit = kept(world)
    third, third_unit = kept(world)
    pulled(world, "admin", "content", second, done=True)
    put(world, world.admin, third, page(15, 30), minutes=-1, device="phone")
    got = progress(world.admin, edition)
    assert got.status_code == 200
    body = got.json()["progress"]
    assert body["unit_id"] == third_unit  # « Reprendre au chapitre N »: the latest touched
    assert body["number"].isdigit() and "unit_title" in body
    assert body["state"] == "in_progress"
    assert body["device"] == "phone"
    assert 0 < body["percent"] < 1
    assert body["last_read_at"]
    assert body["read_count"] == 1  # the second, read; the third is under way
    assert first not in (second_unit, third_unit)


def test_the_progress_is_the_profiles_own_alone(world: World) -> None:  # noqa: F811
    edition = world.seed.editions["ok"]
    content, _unit = kept(world)
    put(world, world.admin, content, page(5), minutes=-1)
    assert progress(world.mum, edition).json()["progress"] is None
    assert progress(world.kid, edition).json()["progress"] is None


def test_a_child_has_no_progress_on_an_edition_she_may_not_see(world: World) -> None:  # noqa: F811
    hidden = world.seed.editions["adult_edition"]  # a visible Work, an Edition above her ceiling
    content, _unit = kept(world, edition="adult_edition")
    pulled(world, "kid", "content", content, done=False)
    shown = progress(world.kid, hidden)
    absent = progress(world.kid, MISSING)
    assert shown.status_code == absent.status_code == 404
    assert shown.json().get("code") == absent.json().get("code")
    assert hidden not in shown.text
    # and the Edition she may see answers with her own reading, never the hidden one's
    assert progress(world.kid, world.seed.editions["ok"]).status_code == 200


def test_the_progress_door_is_the_sessions_and_refuses_a_bad_identifier(world: World) -> None:  # noqa: F811
    anonymous = world.env.other_client("10.0.0.81")
    assert anonymous.get(f"/api/reader/editions/{MISSING}/progress").status_code in (401, 403)
    assert progress(world.admin, "not-an-id").status_code == 404


# -- the home -------------------------------------------------------------------------------


def test_continue_gives_progress_device_and_date(world: World) -> None:  # noqa: F811
    content, unit = kept(world)
    put(world, world.admin, content, page(15, 30), minutes=-3, device="tablet")
    [entry] = items(world.admin.get("/api/reader/continue"))
    assert entry["unit_id"] == unit and entry["state"] == "in_progress"
    assert 0 < entry["percent"] < 1
    assert entry["device"] == "tablet"
    assert entry["last_read_at"]


def test_continue_next_unit_has_no_percent_but_names_the_last_device_and_date(
    world: World,  # noqa: F811
) -> None:
    first, _u1 = kept(world)
    _second, u2 = kept(world)
    pulled(world, "admin", "content", first, done=True)
    [entry] = items(world.admin.get("/api/reader/continue"))
    assert entry["unit_id"] == u2 and entry["state"] == "next"
    assert entry["percent"] is None  # nothing of the next Unit was read
    assert entry["device"] == "server:kavita" and entry["last_read_at"]


def test_continue_read_elsewhere_has_the_same_fields(world: World) -> None:  # noqa: F811
    item, _unit = exported(world)
    read_there(world, "admin", item)
    pulled(world, "admin", "export_item", item, done=False)
    [entry] = items(world.admin.get("/api/reader/continue"))
    assert {"percent", "device", "last_read_at"} <= set(entry)


def test_continue_gives_a_child_nothing_above_her_ceiling_in_the_new_fields(
    world: World,  # noqa: F811
) -> None:
    hidden, _u = kept(world, edition="above")
    put(world, world.admin, hidden, page(9), minutes=-1, device="secret-device")
    pulled(world, "kid", "content", hidden, done=False)
    nothing = world.mum.get("/api/reader/continue")
    got = world.kid.get("/api/reader/continue")
    assert got.json() == nothing.json()
    assert "secret-device" not in got.text and "percent" not in got.text.replace('"items":[]', "")
    visible, _unit = kept(world)
    put(world, world.kid, visible, page(5), minutes=-1, device="kid-phone")
    [entry] = items(world.kid.get("/api/reader/continue"))
    assert entry["device"] == "kid-phone"
    assert world.seed.title_of("above") not in world.kid.get("/api/reader/continue").text
