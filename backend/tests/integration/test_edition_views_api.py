"""The data the series sheet and the profiles register read (EF-USR-006, EF-USR-005, EF-USR-008).

* `GET /api/v1/editions/{id}/units`: the Units of an Edition in reading order, one page at a time,
  with the read state of the profile that asks. The Edition is judged by the one filter of every
  surface (`visible_editions_clause`): an Edition a child may not see answers exactly like one that
  does not exist (404), by session and by key.
* `GET /api/v1/editions/{id}/exports`: what was made of an Edition for the destinations and the
  e-readers. The admin's (403 for anyone else).
* `GET /api/v1/profiles/overview`: how many Works each ceiling shows and, per profile, how many it
  sees, its last reading and its e-readers. The admin's (403 for anyone else).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import count_visible_works
from shelfhold.config import Settings
from shelfhold.identity.public import local_auth, reader_scope, scope_of
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import bundle, export, identity, reading, storage
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_schema_catalog import add_unit
from tests.visibility_env import add_profile

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HASH = "sha256:" + "a" * 64
MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    yield from _world(monkeypatch, settings)


def units_of(world: World, client: Any, key: str, **params: Any) -> Any:
    edition = world.seed.editions.get(key, key)
    return client.get(f"/api/v1/editions/{edition}/units", params=params)


def _shape(response: Any) -> tuple[Any, ...]:
    body = response.json()
    return response.status_code, body.get("type"), body.get("title"), body.get("detail")


def add_units(world: World, key: str, numbers: list[str], **extra: Any) -> dict[str, str]:
    with write_transaction(world.seed.engine) as connection:
        return {n: add_unit(connection, world.seed.editions[key], n, **extra) for n in numbers}


def content_of(world: World, unit_id: str, *, format: str = "cbz") -> str:
    content_id = new_ulid()
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(storage.content).values(
                id=content_id, unit_id=unit_id, variant_id=None, kind="images",
                media_type="application/vnd.comicbook+zip", format=format,
                retention="adopted_ref", path=None, original_sha256="sha256:" + "0" * 64,
                store_provenance="received", size=1,
            )
        )  # fmt: skip
    return content_id


def mark_read(world: World, username: str, unit: str, carrier: str, *, at: datetime = T0) -> None:
    """The journal line of a traversal: the position that closed a move over the Unit."""
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(reading.reading_position).values(
                id=new_ulid(), user_id=user_id_of(world.seed.engine, username),
                carrier_type="content", carrier_id=carrier, content_hash=HASH,
                locator={"page": 3}, percent=None, client_ts=at, server_ts=at,
                source="device", device_id="reader-1", covered_units=[unit],
                read_units=[unit],
            )
        )  # fmt: skip


# -- the Units of an Edition ---------------------------------------------------------------------


@pytest.mark.req("EF-USR-006", "EF-INT-001")
def test_admin_lists_units_in_reading_order_one_page_at_a_time(world: World) -> None:
    """The order of reading (the number, then its segment, then the order of the source), never
    the order of insertion; a page says the total, its bounds and nothing else."""
    made = add_units(world, "ok", ["3.5", "0.75", "0.25"])
    made |= add_units(world, "ok", ["4.5"], title="The End")
    got = units_of(world, world.admin, "ok", limit=2)
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "no-store"
    body = got.json()
    assert set(body) == {"edition_id", "items", "total", "limit", "offset"}
    assert body["edition_id"] == world.seed.editions["ok"]
    assert body["limit"] == 2 and body["offset"] == 0
    total = body["total"]
    assert total >= 4 and len(body["items"]) == 2  # the World's own carriers add Units of theirs
    walked: list[str] = []
    offset = 0
    while offset < total:
        page = units_of(world, world.admin, "ok", limit=2, offset=offset).json()
        walked.extend(i["id"] for i in page["items"])
        offset += 2
    assert len(walked) == len(set(walked)) == total  # nothing repeated, nothing skipped
    order = [walked.index(made[n]) for n in ("0.25", "0.75", "3.5", "4.5")]
    assert order == sorted(order)
    last = units_of(world, world.admin, "ok", limit=200).json()["items"]
    end = next(i for i in last if i["id"] == made["4.5"])
    assert set(end) == {
        "id", "number", "title", "kind", "track", "volume_id", "added_at", "read_state",
        "readable",
    }  # fmt: skip
    assert end["title"] == "The End" and end["kind"] == "chapter" and end["track"] == "main"
    assert end["read_state"] == "unread" and end["readable"] is False
    assert end["volume_id"] is None and end["added_at"]


@pytest.mark.req("EF-USR-006", "EF-LEC-014")
def test_read_state_is_the_asking_profile_own(world: World) -> None:
    """The state is the reader's own: what the child read is not the member's, and the reverse."""
    unit = add_units(world, "ok", ["5.5"])["5.5"]
    carrier = content_of(world, unit)
    mark_read(world, "kid", unit, carrier)

    def state(client: Any) -> tuple[str, bool]:
        items = units_of(world, client, "ok", limit=200).json()["items"]
        row = next(i for i in items if i["id"] == unit)
        return row["read_state"], row["readable"]

    assert state(world.kid) == ("read", True)
    assert state(world.mum) == ("unread", True)
    assert state(world.admin) == ("unread", True)


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_child_under_her_ceiling_sees_the_units(world: World) -> None:
    add_units(world, "ok", ["1.5"])
    got = units_of(world, world.kid, "ok")
    assert got.status_code == 200
    assert "1.5" in [i["number"] for i in got.json()["items"]]


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
@pytest.mark.parametrize("key", ["twelve", "above", "adult_edition", "reserved", "mixed_new"])
def test_child_above_her_ceiling_gets_the_answer_of_an_absent_edition(
    world: World, key: str
) -> None:
    """An Edition above her ceiling, on a shelf that is not hers, or not rated: one 404, the very
    answer of an Edition that does not exist, and none of its Units; by session and by key."""
    hidden = add_units(world, key, ["1.25", "2.25"])
    absent = units_of(world, world.kid, MISSING)
    assert absent.status_code == 404
    got = units_of(world, world.kid, key)
    assert _shape(got) == _shape(absent)
    for unit_id in hidden.values():
        assert unit_id not in got.text
    key_value = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey", username="kid", read_only=True)
        .value
    )
    by_key = world.env.other_client("10.0.0.50").get(
        f"/api/v1/editions/{world.seed.editions[key]}/units",
        headers={"Authorization": f"Bearer {key_value}"},
    )
    assert _shape(by_key) == _shape(absent)
    assert units_of(world, world.admin, key).status_code == 200  # the admin sees it


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_a_higher_ceiling_opens_the_edition_and_a_lowered_one_closes_it_at_once(
    world: World,
) -> None:
    """The same filter as every surface, read from the profile row at each request."""
    add_units(world, "twelve", ["1.25"])
    teen = add_profile(world.env, "teen", "child", "10.0.0.4", max_age_rating=13)
    assert units_of(world, teen, "twelve").status_code == 200
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(identity.user).where(identity.user.c.username == "teen")
            .values(max_age_rating=10)
        )  # fmt: skip
    assert units_of(world, teen, "twelve").status_code == 404


@pytest.mark.req("EF-USR-006")
def test_units_of_an_absent_or_malformed_edition_answer_404(world: World) -> None:
    assert units_of(world, world.admin, MISSING).status_code == 404
    assert units_of(world, world.admin, "not-an-id").status_code == 404


@pytest.mark.req("EF-INT-001")
def test_units_page_bounds_are_checked(world: World) -> None:
    assert units_of(world, world.admin, "ok", limit=0).status_code == 422
    assert units_of(world, world.admin, "ok", limit=201).status_code == 422
    assert units_of(world, world.admin, "ok", offset=-1).status_code == 422
    assert units_of(world, world.admin, "ok", offset=10**30).status_code == 422  # never a 500


# -- the exports of an Edition -------------------------------------------------------------------


def _destination(world: World) -> tuple[str, str]:
    target_id, shelf_id, destination_id = new_ulid(), new_ulid(), new_ulid()
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(id=target_id, kind="kavita", name="Kavita home")
        )
        connection.execute(
            sa.insert(export.shelf).values(id=shelf_id, name="Manga", phrase="manga")
        )
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id, shelf_id=shelf_id, target_id=target_id, transport="tree",
                egress="foyer", template="{series}/{number}", template_origin="default@1",
            )
        )  # fmt: skip
    return destination_id, target_id


def _item(world: World, kind: str, content_id: str | None = None) -> str:
    item_id = new_ulid()
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.export_item).values(
                id=item_id, edition_id=world.seed.editions["ok"], kind=kind, content_id=content_id
            )
        )
    return item_id


def _record(world: World, destination_id: str, item_id: str, state: str, **extra: Any) -> None:
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=destination_id, export_item_id=item_id,
                locator_type="path", rel_path=f"Manga/{item_id}.cbz", state=state, **extra,
            )
        )  # fmt: skip


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_admin_reads_what_was_exported_of_an_edition(world: World) -> None:
    destination_id, target_id = _destination(world)
    epub_unit = add_units(world, "ok", ["7.5"])["7.5"]
    epub = content_of(world, epub_unit, format="epub")
    written = _item(world, "unit", epub)
    planned = _item(world, "unit")
    failed = _item(world, "volume")
    _record(world, destination_id, written, "written", exported_at=T0)
    _record(world, destination_id, planned, "planned")
    _record(world, destination_id, failed, "error", error_code="io")
    kid = user_id_of(world.seed.engine, "kid")
    mum = user_id_of(world.seed.engine, "mum")
    with write_transaction(world.seed.engine) as connection:
        # the block of a private adult is her reading: it is not shown to the admin
        for who in (kid, mum):
            connection.execute(
                sa.insert(bundle.bundle_version).values(
                    id=new_ulid(), edition_id=world.seed.editions["ok"], part_index=1,
                    profile_user_id=who, number=1, content_fp="ab" * 32,
                    koreader_md5="md5:" + "c" * 32, state_key="cd" * 32, size=1234,
                    entry_count=3, page_count=30, member_count=3, created_at=T0,
                )
            )  # fmt: skip
    got = world.admin.get(f"/api/v1/editions/{world.seed.editions['ok']}/exports")
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == {"edition_id", "items", "destinations", "blocks", "kepub_items"}
    assert body["edition_id"] == world.seed.editions["ok"]
    assert body["items"] == {"unit": 2, "volume": 1, "bundle": 0, "adopted": 0, "opds_bundle": 0}
    assert body["destinations"] == [
        {
            "destination_id": destination_id, "target_id": target_id,
            "target_name": "Kavita home", "target_kind": "kavita", "shelf_name": "Manga",
            "enabled": True, "exported": 1, "pending": 1, "failed": 1,
            "last_exported_at": "2026-11-01T12:00:00Z",
        }
    ]  # fmt: skip
    assert body["kepub_items"] == 1  # a live record of an EPUB: what OPDS and /lite offer as KEPUB
    assert body["blocks"] == [
        {
            "profile_id": kid, "username": "kid", "part_index": 1, "number": 1, "size": 1234,
            "created_at": "2026-11-01T12:00:00Z", "last_used_at": None,
        }
    ]  # fmt: skip


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_exports_are_the_admin_s(world: World) -> None:
    path = f"/api/v1/editions/{world.seed.editions['ok']}/exports"
    assert world.kid.get(path).status_code == 403
    assert world.mum.get(path).status_code == 403
    assert world.admin.get(f"/api/v1/editions/{MISSING}/exports").status_code == 404
    empty = world.admin.get(f"/api/v1/editions/{world.seed.editions['twelve']}/exports").json()
    assert empty["destinations"] == [] and empty["blocks"] == [] and empty["kepub_items"] == 0


# -- the profiles register -----------------------------------------------------------------------

OVERVIEW = "/api/v1/profiles/overview"


@pytest.mark.req("EF-USR-005", "EF-USR-006", "ENF-SEC-010")
def test_overview_is_the_admin_s(world: World) -> None:
    """A child and a member are refused (403), by session and by key."""
    assert world.kid.get(OVERVIEW).status_code == 403
    assert world.mum.get(OVERVIEW).status_code == 403
    key_value = (
        local_auth(world.seed.engine, world.env.settings)
        .create_api_key(name="kidkey2", username="kid", read_only=True)
        .value
    )
    by_key = world.env.other_client("10.0.0.51").get(
        OVERVIEW, headers={"Authorization": f"Bearer {key_value}"}
    )
    assert by_key.status_code == 403


@pytest.mark.req("EF-USR-005", "EF-USR-006")
def test_overview_counts_works_per_ceiling_with_the_one_filter(world: World) -> None:
    """Each mark of the rule counts what a child at that ceiling sees (no shelf reserved to her),
    with the very count of the library list: the rule cannot promise more than the list shows."""
    got = world.admin.get(OVERVIEW, params=[("ceiling", 10), ("ceiling", 13), ("ceiling", 0)])
    assert got.status_code == 200, got.text
    assert got.headers["cache-control"] == "no-store"
    body = got.json()
    assert set(body) == {"total_works", "steps", "profiles"}
    with world.seed.engine.connect() as connection:
        expected = {c: count_visible_works(connection, scope_of("child", c)) for c in (0, 10, 13)}
        total = count_visible_works(connection, scope_of("admin", None))
    assert body["steps"] == [{"ceiling": c, "works": expected[c]} for c in (0, 10, 13)]
    assert expected[10] == 3 and expected[13] == 4  # `twelve` is opened by 13, not by 10
    assert body["total_works"] == total == len(world.seed.works)


@pytest.mark.req("EF-USR-005", "EF-USR-006")
def test_overview_steps_for_one_child_count_her_reserved_shelves(world: World) -> None:
    """With `profile_id`, each mark counts what this child would see at that ceiling: the shelves
    reserved to her included, never another child's."""
    kid = user_id_of(world.seed.engine, "kid")
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(export.shelf).where(export.shelf.c.id == world.seed.shelves["family"])
            .values(allowed_profiles=[kid])
        )  # fmt: skip
    mine = world.admin.get(OVERVIEW, params={"ceiling": 10, "profile_id": kid}).json()
    plain = world.admin.get(OVERVIEW, params={"ceiling": 10}).json()
    with world.seed.engine.connect() as connection:
        hers = count_visible_works(connection, scope_of("child", 10, kid))
    assert mine["steps"] == [{"ceiling": 10, "works": hers}]
    assert hers > plain["steps"][0]["works"]  # `reserved` is on her shelf now
    for bad in ("not-an-id", user_id_of(world.seed.engine, "mum")):
        refused = world.admin.get(OVERVIEW, params={"ceiling": 10, "profile_id": bad})
        assert refused.status_code == 422, bad  # only a child's own shelves can be counted


@pytest.mark.req("EF-USR-005", "EF-USR-006", "EF-USR-008", "EF-USR-016")
def test_overview_profiles_carry_their_count_last_reading_and_e_readers(world: World) -> None:
    unit = add_units(world, "ok", ["3.25"])["3.25"]
    carrier = content_of(world, unit)
    mark_read(world, "kid", unit, carrier, at=T0)
    mark_read(world, "kid", unit, carrier, at=T0 + timedelta(hours=1))
    mark_read(world, "mum", unit, carrier, at=T0)  # a private adult: not shown to the admin
    kid = user_id_of(world.seed.engine, "kid")
    kobo, retired = new_ulid(), new_ulid()
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(identity.device).values(
                id=kobo, subject_user_id=kid, label="Kobo Clara", kind="kobo",
                created_at=T0, last_seen_at=T0,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(identity.device).values(
                id=retired, subject_user_id=kid, label="Old", kind="koreader",
                created_at=T0, revoked_at=T0,
            )
        )  # fmt: skip
    body = world.admin.get(OVERVIEW).json()
    profiles = {p["username"]: p for p in body["profiles"]}
    assert set(profiles) >= {"admin", "kid", "mum"}
    kid_row = profiles["kid"]
    assert set(kid_row) == {
        "profile_id",
        "username",
        "role",
        "max_age_rating",
        "works",
        "last_read",
        "devices",
    }
    assert kid_row["profile_id"] == kid and kid_row["max_age_rating"] == 10
    with world.seed.engine.connect() as connection:
        assert kid_row["works"] == count_visible_works(connection, reader_scope(connection, kid))
    assert kid_row["last_read"] == {
        "at": "2026-11-01T13:00:00Z",
        "work_id": world.seed.works["ok"],
        "edition_id": world.seed.editions["ok"],
        "title": world.seed.title_of("ok"),
    }
    assert kid_row["devices"] == [
        {"id": kobo, "label": "Kobo Clara", "kind": "kobo", "last_seen_at": "2026-11-01T12:00:00Z"}
    ]  # the retired one is gone
    assert profiles["mum"]["last_read"] is None  # private: her reading is hers
    # the latest position whose file is gone since does not hide the reading before it
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.insert(reading.reading_position).values(
                id=new_ulid(), user_id=kid, carrier_type="content", carrier_id=new_ulid(),
                content_hash=HASH, locator={"page": 1}, client_ts=T0 + timedelta(hours=2),
                server_ts=T0 + timedelta(hours=2), source="device",
            )
        )  # fmt: skip
    again = {p["username"]: p for p in world.admin.get(OVERVIEW).json()["profiles"]}
    assert again["kid"]["last_read"] == kid_row["last_read"]
    assert profiles["mum"]["works"] == profiles["admin"]["works"] == body["total_works"]
    assert profiles["admin"]["max_age_rating"] is None


@pytest.mark.req("EF-USR-005")
def test_overview_ceiling_bounds_are_checked(world: World) -> None:
    assert world.admin.get(OVERVIEW, params={"ceiling": 100}).status_code == 422
    assert world.admin.get(OVERVIEW, params={"ceiling": -1}).status_code == 422
    many: list[tuple[str, str | int | float | bool | None]] = [("ceiling", c) for c in range(17)]
    assert world.admin.get(OVERVIEW, params=many).status_code == 422
    default = world.admin.get(OVERVIEW).json()
    assert [s["ceiling"] for s in default["steps"]] == [0, 10, 12, 13, 16, 18]
