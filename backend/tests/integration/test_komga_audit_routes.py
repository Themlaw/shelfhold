"""EF-EXP-047, EF-USR-024, ENF-SEC-010 (M4 L08b): the surfaces a Komga audit reaches.

The surfaces are the ones that already list and bind destinations (`/api/v1/shelves/{id}/
destinations`), the admin's status page, and — L22 — the admin's on-demand audit
(`/api/v1/audit/komga-accounts`). What the audit decides must hold there, for everyone who must
not know: a family shelf is bound to a Komga only after the audit proved it, a gap shows to the
admin and to nobody else, and no key and no account name ever comes out of an answer.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.targets.komga import KomgaClient, KomgaKey
from shelfhold.export.targets.komga.audit import KomgaAuditor
from shelfhold.i18n import t_lang
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.fakes.komga import API_KEY, BASE_URL, FakeKomga, administrator, build_fetcher, reader
from tests.integration import test_fan_out_routes as routes
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import UNKNOWN, World
from tests.kavita_env import RecordingEvents
from tests.komga_env import run
from tests.visibility_env import add_profile

LIBRARY = "0KIDSLIB01"
KOMGA_AUDIT = "/api/v1/audit/komga-accounts"
world = routes.world  # the same world as the binding routes: one shelf below 18+, one adult


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
    assert refused.json()["reason"] == "never_audited" and world.count() == 0

    audit(world, target, age=6)  # a child of 6 reads the library of a 13+ shelf
    refused = world.bind(world.family, "kids", target=target)
    assert refused.json()["code"] == "shelf.destination_not_partitioned"
    assert refused.json()["reason"] == "library_above_rating" and world.count() == 0
    assert "secretname" not in refused.text and API_KEY not in refused.text

    fake = audit(world, target, age=13)
    # a Komga binding never owns a tree (J-SIMP-4): the tree of the shelf's own binding is the
    # one it indexes, and the audit that proved the folder is the proof of that binding
    owner = world.bind(world.family, "kids")
    assert owner.status_code == 201, owner.text
    tree = _tree_of_binding(world, owner.json()["id"])
    accepted = world.bind(world.family, "kids", target=target, tree_id=tree)
    assert accepted.status_code == 201, accepted.text
    assert fake.violations == []
    listed = world.api.client.get(world.path(world.family))
    assert listed.status_code == 200
    assert API_KEY not in listed.text and "secretname" not in listed.text
    assert "komga_api_key" not in listed.text and "secret" not in listed.text.lower()

    # another folder than the one audited is not covered by the proof
    other = world.bind(world.family, "elsewhere", target=target, tree_id=tree)
    assert other.json()["code"] == "shelf.destination_not_partitioned"


def _tree_of_binding(world: World, destination_id: str) -> str:
    """The tree a binding owns, read back from the destinations of its shelf."""
    listed = world.api.client.get(world.path(world.family)).json()["destinations"]
    return str(next(item["tree_id"] for item in listed if item["id"] == destination_id))


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
def test_an_api_key_even_read_only_binds_nothing_and_reads_no_binding(
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
        assert post.status_code in {401, 403} and get.status_code in {401, 403}, flags
    assert world.count() == 0


@pytest.mark.req("EF-EXP-047")
def test_a_gap_shows_in_health_to_the_admin_without_a_name_or_a_key(world: World) -> None:
    target = komga_target(world)
    fake = audit(world, target, age=13)
    owner = world.bind(world.family, "kids")
    assert owner.status_code == 201, owner.text
    tree = _tree_of_binding(world, owner.json()["id"])
    accepted = world.bind(world.family, "kids", target=target, tree_id=tree)
    assert accepted.status_code == 201, accepted.text
    fake.users = [administrator(), reader("secretname", [LIBRARY], age=6)]
    client = KomgaClient(build_fetcher(fake), BASE_URL, KomgaKey(API_KEY), clock=world.api.clock)
    run(KomgaAuditor(world.engine, world.api.clock, RecordingEvents()).audit(client, target))
    status = world.api.client.get("/api/v1/status")
    assert status.status_code == 200, status.text
    states = {item["state"] for item in status.json()["waiting"]}
    assert "child_exposure" in states  # the shelf is paused and Health says so
    assert API_KEY not in status.text and "secretname" not in status.text


def komga_index_target(world: World) -> str:
    """A Komga target with an address: the one the audit builds a client for (a row without a
    sealed key is skipped by the factory, never counted clean)."""
    target = new_ulid()
    with write_transaction(world.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind="komga", name="Index", config={"url": BASE_URL},
                path_map=[{"local": str(world.library), "remote": "/manga", "mode": "rw"}],
            )
        )  # fmt: skip
    return target


@pytest.mark.req("EF-EXP-047", "EF-USR-024", "ENF-SEC-010")
def test_the_komga_audit_route_is_the_admins_on_demand_twin(world: World) -> None:
    """L22: `GET /api/v1/audit/komga-accounts` is the last answer of each server, `POST` runs
    the `target.komga_audit` job now — the proof a waiting family link waits for. Both are the
    admin's: a member, a child and an anonymous caller are refused, and no key and no account
    name ever comes out."""
    target = komga_index_target(world)
    last = world.api.client.get(KOMGA_AUDIT)
    assert last.status_code == 200, last.text
    (entry,) = last.json()["targets"]
    assert entry["target_id"] == target and entry["state"] == "never_audited"
    assert entry["reason"] is None and entry["proved"] == 0 and entry["audited_at"] is None
    now = world.api.post(KOMGA_AUDIT)  # the job runs: no sealed key, the server is skipped
    assert now.status_code == 200, now.text
    (entry,) = now.json()["targets"]
    assert entry["state"] == "never_audited"  # a skipped server is never counted clean
    assert API_KEY not in now.text and "secretname" not in now.text
    for role in ("member", "child"):
        extra = {"max_age_rating": 10} if role == "child" else {}
        client = add_profile(world.api, role, role, "10.0.0.6", **extra)
        headers = {"X-CSRF-Token": world.api.csrf(client)}
        assert client.get(KOMGA_AUDIT, headers=headers).status_code == 403
        assert client.post(KOMGA_AUDIT, headers=headers).status_code == 403
    world.api.client.cookies.clear()
    assert world.api.client.get(KOMGA_AUDIT).status_code == 401  # nobody signed in


@pytest.mark.req("EF-EXP-047", "ENF-I18N-001")
def test_a_folder_not_audited_yet_says_when_its_proof_comes(world: World) -> None:
    """L22: the refusal of a family link says, in the language of the admin, that the proof
    will come from the next daily audit (around 7) or from the on-demand one — not the generic
    sentence of the code."""
    target = komga_target(world)
    audit(world, target, age=13)  # /manga/kids is proved; another folder is not
    owner = world.bind(world.family, "kids")
    assert owner.status_code == 201, owner.text
    tree = _tree_of_binding(world, owner.json()["id"])
    refused = world.bind(world.family, "elsewhere", target=target, tree_id=tree)
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["reason"] == "folder_not_audited"
    key = "errors.shelf.destination_not_partitioned.reasons.folder_not_audited"
    fr = t_lang(f"{key}.action", language="fr")
    assert body["action"] == fr and "audit" in fr
    switched = world.api.client.patch(
        "/api/v1/me/preferences", json={"language": "en"},
        headers={"X-CSRF-Token": world.api.csrf()},
    )  # fmt: skip
    assert switched.status_code == 200, switched.text
    again = world.bind(world.family, "elsewhere", target=target, tree_id=tree)
    en = t_lang(f"{key}.action", language="en")
    assert again.json()["action"] == en and en != fr
