"""M5 L14 (EF-LEC-008, EF-USR-006, ENF-SEC-010): « Reprendre la lecture » on the home.

`GET /api/reader/continue`: per profile, the NEXT Unit to read of every series under way, fed alike
by the state pulled from the Kavita relay (`source = server`, on an exported file) and by the
integrated reader (`device`, on the kept copy), read from the `reading_progress` projection. The
visibility of the profile is the one of every other surface: a series she may not see is not
there, whatever a pull reports, and the answer is the one of a profile with no progress at all.
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
from shelfhold.infrastructure.schema import catalog, export, reading
from shelfhold.reading.application.progress import insert_position
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l07_positions import HASH, kept, page, put

pytestmark = pytest.mark.req("EF-LEC-008")

HOME = "/api/reader/continue"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def pulled(world: World, who: str, carrier_type: str, carrier: str, *, done: bool) -> None:
    """What a pull from the relay leaves: a `server` position (the whole Unit when `done`)."""
    with write_transaction(world.seed.engine) as connection:
        insert_position(
            connection, user_id=user_id_of(world.seed.engine, who), carrier_type=carrier_type,
            carrier_id=carrier, content_hash=HASH, locator={"page": 30 if done else 3},
            percent=1.0 if done else 0.1, client_ts=world.env.clock.now() - timedelta(minutes=1),
            device_id="server:kavita", source="server", now=world.env.clock.now(),
        )  # fmt: skip


def items(got: Any) -> list[dict[str, Any]]:
    assert got.status_code == 200, got.text
    return list(got.json()["items"])


def exported(world: World, edition: str = "ok") -> tuple[str, str]:
    """A Unit that only exists as a file sent to a destination (read there): (item id, unit id)."""
    from tests.integration.test_schema_catalog import add_unit

    with write_transaction(world.seed.engine) as connection:
        unit = add_unit(connection, world.seed.editions[edition], "500")
        item = new_ulid()
        connection.execute(
            sa.insert(export.export_item).values(
                id=item, edition_id=world.seed.editions[edition], kind="unit", content_id=None
            )
        )
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip
    return item, unit


def test_continue_reading_home(world: World) -> None:
    """The next Unit of a series under way: the one after the last read, the one being read when
    there is one; one entry per series, whoever said it (the pull, the reader, or both)."""
    assert items(world.admin.get(HOME)) == []  # nothing started: nothing offered
    first, u1 = kept(world)
    second, u2 = kept(world)
    _third, u3 = kept(world)
    pulled(world, "admin", "content", first, done=True)  # read in the other reader, pulled
    got = items(world.admin.get(HOME))
    assert [(i["unit_id"], i["state"], i["target"]) for i in got] == [(u2, "next", "reader")]
    assert got[0]["work_id"] == world.seed.works["ok"] and got[0]["title"]
    assert got[0]["number"].isdigit()  # the number of the Unit, as the catalogue spells it
    put(world, world.admin, second, page(9), minutes=-2)  # then the integrated reader opens it
    got = items(world.admin.get(HOME))
    assert [(i["unit_id"], i["state"]) for i in got] == [(u2, "in_progress")]
    pulled(world, "admin", "content", second, done=True)  # the pull says the same Unit is done
    pulled(world, "admin", "content", second, done=True)  # ... twice
    got = items(world.admin.get(HOME))
    assert [(i["unit_id"], i["state"]) for i in got] == [(u3, "next")]  # no double, moved on
    assert u1 != u3


def test_continue_reading_two_sources_one_entry(world: World) -> None:
    """The pull on an exported file and the reader on the kept copy of the same Unit: one entry."""
    content, unit = kept(world)
    with write_transaction(world.seed.engine) as connection:
        item = new_ulid()
        connection.execute(
            sa.insert(export.export_item).values(
                id=item, edition_id=world.seed.editions["ok"], kind="unit", content_id=content
            )
        )
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item, unit_id=unit, position=1,
                origin="manual",
            )
        )  # fmt: skip
    pulled(world, "admin", "export_item", item, done=False)
    put(world, world.admin, content, page(5), minutes=-1)
    got = items(world.admin.get(HOME))
    assert [i["unit_id"] for i in got] == [unit]


def test_continue_reading_series_finished_is_not_offered(world: World) -> None:
    content, _unit = kept(world)
    pulled(world, "admin", "content", content, done=True)  # the only Unit there is, read
    assert items(world.admin.get(HOME)) == []


def test_continue_reading_is_per_profile(world: World) -> None:
    content, _unit = kept(world)
    put(world, world.admin, content, page(5), minutes=-1)
    assert len(items(world.admin.get(HOME))) == 1
    assert items(world.mum.get(HOME)) == []  # hers is hers: nothing of the admin's
    other = world.admin.get(HOME, params={"user_id": user_id_of(world.seed.engine, "mum")})
    assert [i["unit_id"] for i in items(other)] == [
        i["unit_id"] for i in items(world.admin.get(HOME))
    ]
    mine = world.mum.get(HOME, params={"user_id": user_id_of(world.seed.engine, "admin")})
    assert items(mine) == []  # naming someone else's identifier changes nothing


def test_continue_reading_is_bounded(world: World) -> None:
    for edition in ("ok", "twelve", "adult_shelf", "mixed_rated"):
        content, _unit = kept(world, edition=edition)
        put(world, world.admin, content, page(5), minutes=-1)
    assert len(items(world.admin.get(HOME))) == 4
    assert len(items(world.admin.get(HOME, params={"limit": 2}))) == 2
    assert world.admin.get(HOME, params={"limit": 1000}).status_code == 422  # a ceiling, not a hint
    assert world.admin.get(HOME, params={"limit": 0}).status_code == 422


def test_continue_reading_an_excerpt_makes_no_series_appear(world: World) -> None:
    """An excerpt (D-P1) is read without a position: it writes nothing, so it brings nothing."""
    item, unit = exported(world)
    got = world.admin.get(f"/api/reader/unit/{unit}/publication", params={"scope": "excerpt"})
    assert got.status_code in (200, 409), got.text  # whatever the cut gives, nothing is written
    with world.seed.engine.connect() as connection:
        assert not connection.execute(sa.select(reading.reading_position.c.id)).first()
    assert items(world.admin.get(HOME)) == []
    assert item


def test_continue_reading_opens_kavita_for_an_exported_unit(world: World) -> None:
    """A Unit read where it was sent opens there; the kept copy opens the reader."""
    item, unit = exported(world)
    pulled(world, "admin", "export_item", item, done=False)
    got = items(world.admin.get(HOME))
    assert [(i["unit_id"], i["state"], i["target"]) for i in got] == [
        (unit, "in_progress", "kavita")
    ]
    assert "kavita_url" in world.admin.get(HOME).json()


def test_continue_reading_a_unit_with_no_file_is_not_offered(world: World) -> None:
    """The next Unit that nobody holds cannot be opened: it is not offered, the series waits."""
    content, _unit = kept(world)
    with write_transaction(world.seed.engine) as connection:
        from tests.integration.test_schema_catalog import add_unit

        add_unit(connection, world.seed.editions["ok"], "999")  # a later Unit, no file
    pulled(world, "admin", "content", content, done=True)
    assert items(world.admin.get(HOME)) == []


def test_continue_reading_hides_reclassified_work_from_child(world: World) -> None:
    """A series the child may not see is not there, though a pull reports it: the answer is the
    one for a profile with no progress, byte for byte (no count, no key, no header)."""
    visible, _unit = kept(world, edition="ok")
    kid_id = user_id_of(world.seed.engine, "kid")
    nothing = world.mum.get(HOME)  # the control: a profile that started nothing
    put(world, world.kid, visible, page(5), minutes=-1)
    assert len(items(world.kid.get(HOME))) == 1  # hers while it is hers
    hidden, _u = kept(world, edition="above")  # above her ceiling from the start
    pulled(world, "kid", "content", hidden, done=False)
    assert len(items(world.kid.get(HOME))) == 1
    assert world.seed.title_of("above") not in world.kid.get(HOME).text
    with write_transaction(world.seed.engine) as connection:  # the work is reclassified up
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == world.seed.works["ok"])
            .values(age_rating=16)
        )  # fmt: skip
        connection.execute(
            sa.update(catalog.edition).where(catalog.edition.c.id == world.seed.editions["ok"])
            .values(age_rating=16)
        )  # fmt: skip
    after = world.kid.get(HOME)
    assert after.status_code == nothing.status_code == 200
    assert after.json() == nothing.json()
    assert set(after.headers) - {"set-cookie", "date"} == set(nothing.headers) - {
        "set-cookie",
        "date",
    }
    assert world.seed.title_of("ok") not in after.text
    assert kid_id


def test_continue_reading_refuses_a_key_and_a_device_token(world: World) -> None:
    """The home is the profile's, under its own session: not a key of the API, not a token."""
    app = world.env.app
    token = app.state.auth_gate.auth.create_device_token(subject_username="mum")  # type: ignore[attr-defined]
    import base64

    basic = base64.b64encode(f"{token.public_id}:{token.secret}".encode()).decode()
    got = world.env.other_client("10.0.0.71").get(HOME, headers={"Authorization": f"Basic {basic}"})
    assert got.status_code in (401, 403)
    auth = app.state.auth_gate.auth  # type: ignore[attr-defined]
    key = auth.create_api_key(name="home", username="admin", read_only=True).value
    keyed = world.env.other_client("10.0.0.73").get(
        HOME, headers={"Authorization": f"Bearer {key}"}
    )
    assert keyed.status_code in (401, 403)
    anonymous = world.env.other_client("10.0.0.72").get(HOME)
    assert anonymous.status_code in (401, 403)
