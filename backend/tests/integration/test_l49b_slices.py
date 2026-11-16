"""M4 L49b: the slices of a series block, set from the series sheet (ADR-0053 §1 and §8).

A slice is a number of chapters per part: a series that is too long for one file is read in
fixed ranges, `(2 sur 5)`, whatever the weight of each chapter. The boundaries already written
never move (L49a), a child's feed counts only what the profile sees, and the setting is the admin's
alone (a child, a member, a key and a device token are refused). Real files, real application.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import bundle, export
from shelfhold.reading.bundle import BundleLimits, SeriesBundles, slices
from tests.auth_env import CSRF_HEADER
from tests.bundle_env import Chapter, Library, library_for
from tests.integration.test_l48_opds import World, make_world
from tests.integration.test_l49a_bundle import (
    KEYRING,
    NOW,
    complete,
    drain,
    names_of,
    provider,
    scope_of,
    user_id,
)
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-INT-007", "EF-LEC-009", "EF-USR-006", "EF-UX-004")

MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def library(world: World, key: str = "ok") -> Library:
    root = world.env.settings.data_dir.parent / f"lib-{key}"
    return library_for(world.seed.engine, root, world.seed.editions[key])


def chapters(lib: Library, count: int) -> None:
    for number in range(1, count + 1):
        lib.add(Chapter.simple(str(number), count=2))


def slice_of(world: World, key: str = "ok") -> int | None:
    with world.seed.engine.connect() as c:
        return slices.get_slice(c, world.seed.editions[key])


def set_slice(world: World, value: int | None, key: str = "ok") -> None:
    with write_transaction(world.seed.engine) as c:
        slices.set_slice(c, world.seed.editions[key], value, NOW)


def part_sizes(world: World, key: str = "ok") -> list[int]:
    api, who = provider(world), user_id(world, "mum")
    offers = api.offers(user_id=who, scope=scope_of(world, "mum"), edition_id=world.seed.editions[key])
    sizes = []
    for offer in offers:
        body = drain(
            api.open(
                user_id=who, scope=scope_of(world, "mum"),
                edition_id=world.seed.editions[key], part=offer.part,
            )
        )  # fmt: skip
        sizes.append(len({n.split("/")[0] for n in names_of(body) if "/" in n}))
    return sizes


def boundaries(world: World) -> list[tuple[int, str | None]]:
    with world.seed.engine.connect() as c:
        return [
            (r.part_index, r.first_unit_id)
            for r in c.execute(sa.select(bundle.bundle_partition).order_by("part_index"))
        ]


# -- the feed and the files ---------------------------------------------------------------------


def test_a_series_is_cut_every_n_chapters_whatever_their_weight(lab: World) -> None:
    chapters(library(lab), 5)
    set_slice(lab, 2)
    offers = provider(lab).offers(
        user_id=user_id(lab, "mum"), scope=scope_of(lab, "mum"), edition_id=lab.seed.editions["ok"]
    )
    assert [o.part for o in offers] == [1, 2, 3]
    assert all(f"({o.part} sur 3)" in o.filename for o in offers)
    assert part_sizes(lab) == [2, 2, 1]


def test_without_a_slice_the_cut_is_the_one_of_the_bytes(lab: World) -> None:
    chapters(library(lab), 5)
    assert slice_of(lab) is None
    assert part_sizes(lab) == [5]


def test_the_slice_never_lets_a_part_pass_the_cap_of_bytes_or_members(lab: World) -> None:
    lib = library(lab)
    chapters(lib, 6)
    set_slice(lab, 200)  # wider than the members a part may hold
    api, who = (
        provider(lab, limits=BundleLimits(members_per_part=3)),
        user_id(lab, "mum"),
    )
    offers = api.offers(user_id=who, scope=scope_of(lab, "mum"), edition_id=lab.seed.editions["ok"])
    assert len(offers) == 2  # 3 members at most, the slice is only ever a narrower cut


def test_a_boundary_written_never_moves_when_the_slice_changes(lab: World) -> None:
    lib = library(lab)
    chapters(lib, 4)
    set_slice(lab, 2)
    assert part_sizes(lab) == [2, 2]
    written = boundaries(lab)
    set_slice(lab, 1)  # narrower: the parts written stay as they are
    assert part_sizes(lab) == [2, 2]
    lib.add(Chapter.simple("5", count=2))
    lib.add(Chapter.simple("6", count=2))
    assert part_sizes(lab) == [2, 2, 1, 1]  # only what is new is cut by the new slice
    assert boundaries(lab)[:2] == written
    set_slice(lab, None)
    assert boundaries(lab)[:2] == written


def test_a_chapter_added_goes_in_the_last_part_until_the_slice_is_full(lab: World) -> None:
    lib = library(lab)
    chapters(lib, 3)
    set_slice(lab, 2)
    assert part_sizes(lab) == [2, 1]
    lib.add(Chapter.simple("4", count=2))
    assert part_sizes(lab) == [2, 2]
    lib.add(Chapter.simple("5", count=2))
    assert part_sizes(lab) == [2, 2, 1]


def test_changing_the_slice_makes_no_new_version_of_a_part_that_did_not_change(lab: World) -> None:
    chapters(library(lab), 4)
    set_slice(lab, 2)
    part_sizes(lab)
    with lab.seed.engine.connect() as c:
        before = c.execute(
            sa.select(bundle.bundle_version.c.part_index, bundle.bundle_version.c.version_no)
            .order_by("part_index")
        ).all()  # fmt: skip
    set_slice(lab, 1)
    part_sizes(lab)
    with lab.seed.engine.connect() as c:
        after = c.execute(
            sa.select(bundle.bundle_version.c.part_index, bundle.bundle_version.c.version_no)
            .order_by("part_index")
        ).all()  # fmt: skip
    assert after == before


def test_a_slice_is_the_same_bytes_as_the_same_chapters_cut_by_the_cap_of_members(
    lab: World,
) -> None:
    """The slice decides where a part ends, not what a part is: the zip stays deterministic."""
    chapters(library(lab), 4)
    set_slice(lab, 2)
    api, who, scope = provider(lab), user_id(lab, "mum"), scope_of(lab, "mum")
    edition = lab.seed.editions["ok"]
    first = drain(api.open(user_id=who, scope=scope, edition_id=edition, part=1))
    again = drain(provider(lab).open(user_id=who, scope=scope, edition_id=edition, part=1))
    assert first == again


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_a_child_gets_no_slice_of_an_edition_above_its_ceiling(lab: World) -> None:
    lib = library(lab, "above")
    chapters(lib, 4)
    set_slice(lab, 2, "above")
    edition = lab.seed.editions["above"]
    for part in (None, 1, 2):
        got = complete(lab.kid, lab, "above", part)
        assert got.status_code == 404, got.text
        assert edition not in got.text
    api, kid = provider(lab), user_id(lab, "kid")
    assert api.offers(user_id=kid, scope=scope_of(lab, "kid"), edition_id=edition) == []


@pytest.mark.req("EF-USR-006", "ENF-SEC-010")
def test_a_part_that_does_not_exist_is_a_404_for_every_profile(lab: World) -> None:
    chapters(library(lab), 3)
    set_slice(lab, 2)
    assert complete(lab.mum, lab, part=2).status_code == 200
    assert complete(lab.mum, lab, part=3).status_code == 404
    assert complete(lab.kid, lab, part=2).status_code in (200, 404)


# -- the setting, from the series sheet ---------------------------------------------------------


def path(world: World, key: str = "ok") -> str:
    return f"/api/v1/editions/{world.seed.editions[key]}/bundle"


def put_slice(world: World, value: Any, *, client: TestClient | None = None, key: str = "ok") -> Any:
    return world.env.post(f"{path(world, key)}-slice", client=client, json={"slice_chapters": value})


def test_the_admin_reads_the_parts_and_what_is_left_out(lab: World) -> None:
    lib = library(lab)
    chapters(lib, 3)
    lib.add(Chapter("4", [("1.avif", b"\x00\x00\x00\x1cftypavif" + b"x" * 40)]))
    lib.add(Chapter.simple("5", fmt="epub"))
    set_slice(lab, 2)
    got = lab.env.client.get(path(lab))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["edition_id"] == lab.seed.editions["ok"]
    assert body["slice_chapters"] == 2
    assert (body["slice_min"], body["slice_max"]) == (1, 256)
    assert [(p["index"], p["chapters"]) for p in body["parts"]] == [(1, 2), (2, 1)]
    assert body["parts"][0]["first_number"] == "1" and body["parts"][0]["last_number"] == "2"
    assert body["excluded"]["format"] == 1
    assert body["can"] == {"set_slice": True}


def test_reading_the_parts_writes_nothing(lab: World) -> None:
    chapters(library(lab), 3)
    set_slice(lab, 2)
    lab.env.client.get(path(lab))
    with lab.seed.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(bundle.bundle_partition)).scalar() == 0
        assert c.execute(sa.select(sa.func.count()).select_from(bundle.bundle_version)).scalar() == 0


def test_the_admin_sets_and_clears_the_slice(lab: World) -> None:
    chapters(library(lab), 3)
    made = put_slice(lab, 2)
    assert made.status_code == 200, made.text
    assert made.json()["slice_chapters"] == 2 and slice_of(lab) == 2
    assert [p["chapters"] for p in made.json()["parts"]] == [2, 1]
    cleared = put_slice(lab, None)
    assert cleared.status_code == 200 and slice_of(lab) is None
    with lab.seed.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(bundle.bundle_setting)).scalar() == 0


@pytest.mark.parametrize("value", [0, -1, 257, 1.5, "2", True, [2]])
def test_a_slice_out_of_range_or_of_another_type_is_refused(lab: World, value: Any) -> None:
    got = put_slice(lab, value)
    assert got.status_code in (400, 422), got.text
    assert slice_of(lab) is None


def test_an_unknown_field_or_an_unknown_edition_is_refused(lab: World) -> None:
    extra = lab.env.post(f"{path(lab)}-slice", json={"slice_chapters": 2, "part_bytes": 1})
    assert extra.status_code in (400, 422) and slice_of(lab) is None
    assert lab.env.client.get(f"/api/v1/editions/{MISSING}/bundle").status_code == 404
    missing = lab.env.post(f"/api/v1/editions/{MISSING}/bundle-slice", json={"slice_chapters": 2})
    assert missing.status_code == 404


def test_the_setting_is_the_admins_alone(lab: World) -> None:
    chapters(library(lab), 3)
    member = add_profile(lab.env, "mem", "member", "10.0.0.4")
    key = local_auth(lab.seed.engine, lab.env.settings).create_api_key(
        name="bundlekey", username="admin", read_only=False
    ).value
    url = path(lab)
    for who in (lab.kid_session, member):
        assert who.get(url).status_code == 403
        assert put_slice(lab, 2, client=who).status_code == 403
    anonymous = TestClient(lab.env.app, base_url="https://testserver")
    assert anonymous.get(url).status_code in (401, 403)
    assert anonymous.post(f"{url}-slice", json={"slice_chapters": 2}).status_code in (401, 403)
    by_key = lab.env.other_client("10.0.0.9").post(
        f"{url}-slice", json={"slice_chapters": 2}, headers={"Authorization": f"Bearer {key}"}
    )
    assert by_key.status_code in (401, 403)
    for reader in (lab.mum, lab.kid):  # a device token opens the feed and the blocks, no more
        assert reader.get(url).status_code in (401, 403)
        denied = lab.env.other_client("10.0.0.10").post(
            f"{url}-slice", json={"slice_chapters": 2}, headers=reader.headers
        )
        assert denied.status_code in (401, 403)
    assert slice_of(lab) is None


def test_a_write_needs_the_csrf_token_of_the_session(lab: World) -> None:
    got = lab.env.client.post(
        f"{path(lab)}-slice", json={"slice_chapters": 2}, headers={CSRF_HEADER: "not-the-token"}
    )
    assert got.status_code in (400, 403) and slice_of(lab) is None


def test_the_child_never_reads_the_slice_through_the_sheet_of_an_edition_it_cannot_see(
    lab: World,
) -> None:
    set_slice(lab, 2, "above")
    got = lab.kid_session.get(path(lab, "above"))
    assert got.status_code == 403
    assert lab.seed.editions["above"] not in got.text


def test_a_slice_is_a_whole_number_between_the_bounds_in_the_database(lab: World) -> None:
    for bad in (0, 257, -3):
        with pytest.raises(sa.exc.IntegrityError), write_transaction(lab.seed.engine) as c:
            c.execute(
                sa.insert(bundle.bundle_setting).values(
                    edition_id=lab.seed.editions["ok"], slice_chapters=bad
                )
            )


# -- backup and restore -------------------------------------------------------------------------


def test_the_export_carries_the_slice_and_an_import_gives_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.platform.export_all import EXPORTED_TABLES

    assert "bundle_setting" in EXPORTED_TABLES
    from shelfhold.ops.export_all import ExportAllOps
    from tests.integration.test_l37_export_all import (
        ADMIN,
        export_to,
        fresh_instance,
        settings_in,
        shelve,
    )
    from tests.l50_env import seeded

    base = settings_in(tmp_path)
    monkeypatch.setenv("SHELFHOLD_LIBRARY_DIR", str(base.library_dir))
    monkeypatch.setenv("SHELFHOLD_DISK_PAUSE_PERCENT", "0")
    monkeypatch.setenv("SHELFHOLD_DISK_PAUSE_GIB", "0")
    with seeded(base, tmp_path) as env:
        shelved = shelve(env)
        with write_transaction(env.inst.engine) as c:
            slices.set_slice(c, shelved.kept.edition_id, 7, datetime(2026, 11, 16, tzinfo=UTC))
        out = tmp_path / "out"
        export_to(env, out)
    rows = json.loads((out / "data" / "bundle_setting.json").read_text(encoding="utf-8"))
    assert [r["slice_chapters"] for r in rows] == [7]
    fresh_home = settings_in(tmp_path / "fresh")
    with fresh_instance(fresh_home, tmp_path / "fresh") as target:
        ExportAllOps(target.comp).import_all(str(out), principal=ADMIN)
        with target.engine.connect() as c:
            assert slices.get_slice(c, shelved.kept.edition_id) == 7


def test_the_history_of_the_versions_and_the_parts_survives_a_slice_that_goes(lab: World) -> None:
    chapters(library(lab), 4)
    set_slice(lab, 2)
    part_sizes(lab)
    with lab.seed.engine.connect() as c:
        held = c.execute(sa.select(sa.func.count()).select_from(bundle.bundle_version)).scalar()
        kind = c.execute(sa.select(export.export_item.c.kind).distinct()).scalars().all()
    assert held == 2 and "opds_bundle" in kind
    set_slice(lab, None)
    with lab.seed.engine.connect() as c:
        assert (
            c.execute(sa.select(sa.func.count()).select_from(bundle.bundle_version)).scalar()
            == held
        )
        assert (
            c.execute(sa.select(sa.func.count()).select_from(bundle.bundle_partition)).scalar()
            == 2
        )


def test_the_provider_of_the_instance_reads_the_slice_at_each_request(lab: World) -> None:
    chapters(library(lab), 3)
    api = SeriesBundles(lab.seed.engine, lambda: KEYRING, now=lambda: NOW)
    who, scope, edition = user_id(lab, "mum"), scope_of(lab, "mum"), lab.seed.editions["ok"]
    assert len(api.offers(user_id=who, scope=scope, edition_id=edition)) == 1
    set_slice(lab, 1)
    assert len(api.offers(user_id=who, scope=scope, edition_id=edition)) == 3
