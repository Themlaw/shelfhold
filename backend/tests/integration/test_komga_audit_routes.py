"""EF-EXP-047, EF-USR-024, ENF-SEC-010 (M4 L08b): the surfaces a Komga audit reaches.

No route nor command creates a Komga target in this lot: the surfaces are the ones that already
list and bind destinations (`/api/v1/shelves/{id}/destinations`) and the admin's status page. What
the audit decides must hold there, for everyone who must not know: a family shelf is bound to a
Komga only after the audit proved it, a gap shows to the admin and to nobody else, and no key and
no account name ever comes out of an answer.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.targets.komga import KomgaClient, KomgaKey
from shelfhold.export.targets.komga.audit import KomgaAuditor
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.fakes.komga import API_KEY, BASE_URL, FakeKomga, administrator, build_fetcher, reader
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import UNKNOWN, World, world  # noqa: F401
from tests.kavita_env import RecordingEvents
from tests.komga_env import run
from tests.visibility_env import add_profile

LIBRARY = "0KIDSLIB01"


def komga_target(world: World) -> str:
    target = new_ulid()
    with write_transaction(world.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind="komga", name="Komga",
                path_map=[{"local": str(world.library), "remote": "/manga", "mode": "rw"}],
            )
        )  # fmt: skip
    return target


def audit(world: World, target: str, *, age: int) -> FakeKomga:
    fake = FakeKomga(
        libraries=[{"id": LIBRARY, "name": "Kids", "root": "/manga/kids", "unavailable": False}],
        users=[administrator(), reader("secretname", [LIBRARY], age=age)],
    )
    client = KomgaClient(build_fetcher(fake), BASE_URL, KomgaKey(API_KEY), clock=world.api.clock)
    auditor = KomgaAuditor(world.engine, world.api.clock, RecordingEvents())
    run(auditor.admit(client, target, shelf_ceiling=13, remote_root="/manga/kids"))
    return fake


@pytest.mark.req("EF-EXP-047")
def test_a_family_shelf_is_bound_to_komga_only_after_the_audit_proved_it(world: World) -> None:
    target = komga_target(world)
    refused = world.bind(world.family, "kids", target=target)  # nobody audited
    assert refused.status_code in {403, 409, 422}
    assert refused.json()["code"] == "shelf.destination_not_partitioned"
    assert "never_audited" in refused.text and world.count() == 0

    audit(world, target, age=6)  # a child of 6 reads the library of a 13+ shelf
    refused = world.bind(world.family, "kids", target=target)
    assert refused.json()["code"] == "shelf.destination_not_partitioned"
    assert "library_above_rating" in refused.text and world.count() == 0
    assert "secretname" not in refused.text and API_KEY not in refused.text

    fake = audit(world, target, age=13)
    accepted = world.bind(world.family, "kids", target=target)
    assert accepted.status_code == 201, accepted.text
    assert fake.violations == []
    listed = world.api.client.get(world.path(world.family))
    assert listed.status_code == 200
    assert API_KEY not in listed.text and "secretname" not in listed.text
    assert "komga_api_key" not in listed.text and "secret" not in listed.text.lower()

    # another folder than the one audited is not covered by the proof
    other = world.bind(world.family, "elsewhere", target=target)
    assert other.json()["code"] == "shelf.destination_not_partitioned"


@pytest.mark.req("EF-EXP-047", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["child", "member"])
def test_a_child_and_a_member_learn_nothing_of_the_audit_and_bind_nothing(
    world: World, role: str
) -> None:
    target = komga_target(world)
    audit(world, target, age=13)  # proved: the best case for the attacker
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    headers = {"X-CSRF-Token": world.api.csrf(client)}
    (world.library / "kids").mkdir(exist_ok=True)
    body = {"target_id": target, "root_subpath": "kids"}
    for shelf in (world.family, world.adult, UNKNOWN):  # IDOR on someone else's shelf
        post = client.post(world.path(shelf), json=body, headers=headers)
        get = client.get(world.path(shelf))
        assert post.status_code == 403 and get.status_code == 403, (shelf, post.text, get.text)
        assert post.json()["code"] == "forbidden" == get.json()["code"]
        assert "audit" not in post.text.lower() and "never_audited" not in post.text
    status = client.get("/api/v1/status")
    assert status.status_code == 403  # the page of the gaps is the admin's
    assert world.count() == 0


@pytest.mark.req("EF-EXP-047", "EF-USR-024", "ENF-SEC-010")
def test_an_api_key_even_read_only_binds_nothing_and_reads_no_gap(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = komga_target(world)
    audit(world, target, age=13)
    (world.library / "kids").mkdir(exist_ok=True)
    body = {"target_id": target, "root_subpath": "kids"}
    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        other = world.api.other_client("10.0.0.9")
        post = other.post(world.path(world.family), json=body, headers=_bearer(key))
        get = other.get(world.path(world.family), headers=_bearer(key))
        status = other.get("/api/v1/status", headers=_bearer(key))
        assert post.status_code in {401, 403} and get.status_code in {401, 403}, flags
        assert status.status_code in {401, 403}, flags
    assert world.count() == 0


@pytest.mark.req("EF-EXP-047")
def test_a_gap_shows_in_health_to_the_admin_without_a_name_or_a_key(world: World) -> None:
    target = komga_target(world)
    fake = audit(world, target, age=13)
    accepted = world.bind(world.family, "kids", target=target)
    assert accepted.status_code == 201, accepted.text
    fake.users = [administrator(), reader("secretname", [LIBRARY], age=6)]
    client = KomgaClient(build_fetcher(fake), BASE_URL, KomgaKey(API_KEY), clock=world.api.clock)
    run(KomgaAuditor(world.engine, world.api.clock, RecordingEvents()).audit(client, target))
    status = world.api.client.get("/api/v1/status")
    assert status.status_code == 200, status.text
    states = {item["state"] for item in status.json()["waiting"]}
    assert "child_exposure" in states  # the shelf is paused and Health says so
    assert API_KEY not in status.text and "secretname" not in status.text
