"""Second avis indépendant sur M5 L02 : le registre des appareils et la rotation du secret.

Relecture adverse de la branche (après #347 et son contradicteur), sans toucher au code de
production. La liste de chasse : un jeton qui survivrait à une révocation, une rotation, le retrait
de l'appareil, un changement de rôle, une réinitialisation de mot de passe ou la suppression du
profil ; l'ancien et le nouveau secret valides en même temps ; deux rotations qui se coursent
(le compare-and-swap, `409 device_rotation_conflict`) ; l'énumération des appareils d'un autre
compte ; la « vue pour la dernière fois » qui écrirait à chaque requête ; un secret qui se
re-montrerait au rejeu d'une rotation.

Chaque test ci-dessous est une attaque jouée à travers l'application réelle (sessions réelles,
CSRF réel, horloge déplacée à la main). Aucune n'a abouti : aucune n'est restée rouge, elles
tiennent donc ici en vert, pour garder fermées les portes qu'elles ont essayées — la fermeture des
appareils par les événements du profil (réinitialisation, changement de rôle, suppression), la
fenêtre d'écriture de la « dernière vue » et les rotations qui se coursent avec une révocation ou
entre elles n'avaient pas de test d'intégration au niveau du registre. Les observations qui
restent (défense en profondeur, robustesse, sans chemin par l'application) sont dans le compte
rendu de la relecture.
"""

import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, db_rows, run_cli
from tests.integration.test_l46_device_tokens import KOSYNC_AUTH, OPDS_PATH, basic, kosync
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-USR-008", "ENF-SEC-013")

DEVICES = "/api/v1/devices"
TOKENS = "/api/v1/device-tokens"
PROFILES = "/api/v1/profiles"


class World:
    """The household of the review: an admin, a member, a child, their real sessions."""

    def __init__(self, env: AuthEnv) -> None:
        self.env = env
        self.admin = env.client
        assert env.login().status_code == 200
        self.mum = add_profile(env, "mum", "member", "10.0.0.2")
        self.kid = add_profile(env, "kid", "child", "10.0.0.4", max_age_rating=10)
        self.ids = {
            name: str(db_rows(env.settings, "SELECT id FROM user WHERE username=?", name)[0][0])
            for name in ("admin", "mum", "kid")
        }

    def send(self, client: TestClient, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        headers = {CSRF_HEADER: self.env.csrf(client), **kw.pop("headers", {})}
        return client.request(method, path, headers=headers, json=body, **kw)

    def pair(self, client: TestClient, subject: str | None = None, **extra: Any) -> Any:
        body: dict[str, Any] = {**extra}
        if subject is not None:
            body["subject_user_id"] = self.ids[subject]
        return self.send(client, "POST", TOKENS, body)

    def paired(
        self, client: TestClient, subject: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        made = self.pair(client, subject, **extra)
        assert made.status_code == 201, made.text
        return dict(made.json())

    def rotate(self, client: TestClient, device_id: str, body: Any = None, **kw: Any) -> Any:
        return self.send(client, "POST", f"{DEVICES}/{device_id}/rotate", body, **kw)

    def remove(self, client: TestClient, device_id: str) -> Any:
        return self.send(client, "DELETE", f"{DEVICES}/{device_id}")

    def kosync_status(self, public_id: str, secret: str) -> int:
        return int(self.admin.get(KOSYNC_AUTH, headers=kosync(public_id, secret)).status_code)

    def rows(self, sql: str, *params: object) -> list[tuple[object, ...]]:
        return db_rows(self.env.settings, sql, *params)

    def rotations_audited(self) -> int:
        counted = self.rows("SELECT count(*) FROM audit WHERE action = 'device_token.rotated'")
        return int(counted[0][0])


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as env:
        yield World(env)


def _gate_rotations(world: World, monkeypatch: pytest.MonkeyPatch, before_write: Any) -> None:
    """The service reads its device, then reaches the store: `before_write` runs there, once per
    call, so that a test decides what happens between the read and the write."""
    store = world.env.app.state.auth_gate.auth._store  # type: ignore[attr-defined]
    real = store.rotate_device_secret

    def gated(*args: Any, **kwargs: Any) -> Any:
        before_write()
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "rotate_device_secret", gated)


# --- the events of a profile close its devices, not only its tokens -----------------------------


@pytest.mark.req("EF-USR-011", "EF-USR-008")
def test_a_password_reset_retires_the_device_of_the_profile(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stolen e-reader is the reason of `admin reset-password`: the tokens close, and the
    registry must not keep listing an e-reader that nothing can open."""
    made = world.paired(world.admin, "kid", name="Kid's e-reader")
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    secret = world.env.settings.data_dir.parent / "reset.password"
    secret.write_text("another correct horse battery staple\n", encoding="utf-8")
    secret.chmod(0o600)
    assert (
        run_cli(
            monkeypatch, world.env.settings, "admin", "reset-password", "--username", "kid",
            "--password-file", str(secret),
        )
        == 0
    )  # fmt: skip
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    assert world.rows(
        "SELECT d.revoked_at IS NOT NULL, t.revoked_at IS NOT NULL FROM device d "
        "JOIN device_token t ON t.device_id = d.id"
    ) == [(1, 1)], "the registry and the token close together"
    listed = world.admin.get(DEVICES).json()["items"]
    assert made["device_id"] not in {i["id"] for i in listed}, "the registry lists nothing dead"


@pytest.mark.req("EF-USR-022", "ENF-SEC-010", "EF-USR-008")
def test_a_role_change_retires_the_device_and_its_token(world: World) -> None:
    """A member become child (or the other way) starts again: no e-reader of the old role, and
    the audit says how many tokens that closed."""
    made = world.paired(world.mum, name="Mum's")
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    changed = world.send(
        world.admin, "PATCH", f"{PROFILES}/{world.ids['mum']}",
        {"role": "child", "max_age_rating": 10},
    )  # fmt: skip
    assert changed.status_code == 200, changed.text
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    assert made["device_id"] not in {i["id"] for i in world.admin.get(DEVICES).json()["items"]}
    assert world.rows(
        "SELECT count(*) FROM audit WHERE action = 'device_token.revoked_role_change'"
    ) == [(1,)]


@pytest.mark.req("EF-USR-013", "EF-USR-008")
def test_a_deleted_profile_takes_its_devices_and_tokens_with_it(world: World) -> None:
    """Deleting the profile deletes the rows (the foreign keys cascade), and what read as that
    profile opens nothing: the token is gone, not merely refused."""
    made = world.paired(world.admin, "kid", name="Kid's e-reader")
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    gone = world.send(world.admin, "DELETE", f"{PROFILES}/{world.ids['kid']}")
    assert gone.status_code in (200, 204), gone.text
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    assert world.rows("SELECT count(*) FROM device") == [(0,)]
    assert world.rows("SELECT count(*) FROM device_token") == [(0,)]


# --- the "seen for the last time" is a slow counter, not a write per request ---------------------


@pytest.mark.req("EF-USR-008")
def test_last_seen_moves_once_per_minute_never_per_request(world: World) -> None:
    """L46's write interval holds for the registry too: two requests inside the minute move
    nothing, the one after the minute does. A counter that wrote at each request would be a
    write amplifier and a finer-grained activity trace than the feature promises."""
    made = world.paired(world.mum, kind="koreader")
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    first = world.rows("SELECT last_seen_at FROM device WHERE id = ?", made["device_id"])[0][0]
    assert first is not None
    world.env.clock.advance(timedelta(seconds=30))
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    within = world.rows("SELECT last_seen_at FROM device WHERE id = ?", made["device_id"])[0][0]
    assert within == first, "a request inside the interval writes nothing"
    world.env.clock.advance(timedelta(seconds=31))
    assert world.kosync_status(made["public_id"], made["secret"]) == 200
    moved = world.rows("SELECT last_seen_at FROM device WHERE id = ?", made["device_id"])[0][0]
    assert moved != first, "the first request after the interval moves it"


# --- a rotation that races a revocation shows nothing and writes nothing ------------------------


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_a_rotation_that_races_the_removal_shows_no_secret_and_writes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device is read live, then removed before the write: the compare-and-swap must find it
    gone, show no secret and leave no audit line — a removed e-reader is not rotated back to
    life, not even for the length of one request."""
    made = world.paired(world.mum, name="Kobo")

    def removed_meanwhile() -> None:
        assert world.remove(world.mum, made["device_id"]).status_code == 204

    _gate_rotations(world, monkeypatch, removed_meanwhile)
    answer = world.rotate(world.mum, made["device_id"])
    assert answer.status_code == 404, answer.text
    assert answer.json()["code"] == "device_token_not_found"
    assert "secret" not in answer.json()
    assert world.rotations_audited() == 0


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_a_rotation_that_races_the_old_route_revoke_shows_no_secret_and_writes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same race with the revocation of the token by its old route, which also retires the
    device when it was its last live token: the rotation answers like a device that is not
    there, and nothing is written."""
    made = world.paired(world.mum, name="Kobo")

    def revoked_meanwhile() -> None:
        assert world.send(world.mum, "DELETE", f"{TOKENS}/{made['public_id']}").status_code == 204

    _gate_rotations(world, monkeypatch, revoked_meanwhile)
    answer = world.rotate(world.mum, made["device_id"])
    assert answer.status_code == 404, answer.text
    assert "secret" not in answer.json()
    assert world.rotations_audited() == 0
    assert world.kosync_status(made["public_id"], made["secret"]) == 401


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_two_rotations_with_opposite_expiry_wishes_leave_one_winner_one_expiry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rotations of one device, one asking a new expiry and the other none, read the same
    state before either writes: only the winner exists — one secret shown, one audit line, and
    the expiry is the winner's choice, never a mix of both."""
    made = world.paired(world.mum, name="Kobo", expires_days=90)
    before = world.rows(
        "SELECT expires_at FROM device_token WHERE public_id = ?", made["public_id"]
    )[0][0]
    twin = world.env.other_client("10.0.0.62")
    assert world.env.login("mum", client=twin).status_code == 200
    barrier = threading.Barrier(2, timeout=20)
    _gate_rotations(world, monkeypatch, barrier.wait)  # both have read before either writes
    answers: list[tuple[bool, Any]] = []

    def call(client: TestClient, body: Any) -> None:
        answers.append((bool(body), world.rotate(client, made["device_id"], body)))

    threads = [
        threading.Thread(target=call, args=(c, b))
        for c, b in ((world.mum, {"expires_days": 30}), (twin, None))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(a.status_code for _, a in answers) == [200, 409], [a.text for _, a in answers]
    won = next(a for _, a in answers if a.status_code == 200)
    lost = next(a for _, a in answers if a.status_code == 409)
    assert lost.json()["code"] == "device_rotation_conflict"
    assert "secret" not in lost.json()
    asked_days = next(asked for asked, a in answers if a.status_code == 200)
    after = world.rows(
        "SELECT expires_at FROM device_token WHERE public_id = ?", made["public_id"]
    )[0][0]
    if asked_days:  # the winner's window stands, alone
        assert after != before
    else:  # nothing moved: the expiry the pairing set is kept
        assert after == before
    assert world.rotations_audited() == 1, "a refused rotation leaves no line"
    assert world.kosync_status(won.json()["public_id"], won.json()["secret"]) == 200
    assert world.kosync_status(made["public_id"], made["secret"]) == 401, "the old one is dead"


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_a_replayed_rotation_never_shows_a_secret_that_is_already_dead(world: World) -> None:
    """A rotation is a reserved action: its answer is kept out of the replay store of
    `Idempotency-Key`. A retry with the same key must not hand back the secret of the first
    rotation — the second rotation killed it — but a secret that lives, or none at all."""
    made = world.paired(world.mum, name="Kobo")
    key = {"Idempotency-Key": "rotate-retry-0001"}
    first = world.rotate(world.mum, made["device_id"], headers=key)
    assert first.status_code == 200, first.text
    second = world.rotate(world.mum, made["device_id"], headers=key)
    assert second.status_code in (200, 409, 422), second.text
    if second.status_code == 200:  # the answer was not replayed: it is a new rotation
        assert second.json()["secret"] != first.json()["secret"]
        assert world.kosync_status(first.json()["public_id"], first.json()["secret"]) == 401
        assert world.kosync_status(second.json()["public_id"], second.json()["secret"]) == 200
    else:  # refused (the session went stale, a conflict): the first secret still lives
        assert world.kosync_status(first.json()["public_id"], first.json()["secret"]) == 200


# --- the secret of a rotation is dead on both doors the moment it is born -----------------------


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_the_secret_before_the_rotation_is_refused_on_both_doors_at_once(world: World) -> None:
    """No grace period (D19): the request that follows the rotation is refused with the old
    secret on the OPDS door and on the kosync door alike, and the new secret opens both."""
    made = world.paired(world.mum, kind="kobo")
    turned = world.rotate(world.mum, made["device_id"]).json()
    alone = world.env.other_client("10.0.0.61")
    assert alone.get(OPDS_PATH, headers=basic(made["public_id"], made["secret"])).status_code == 401
    assert world.kosync_status(made["public_id"], made["secret"]) == 401
    opened = alone.get(OPDS_PATH, headers=basic(turned["public_id"], turned["secret"]))
    assert opened.status_code == 200
    assert world.kosync_status(turned["public_id"], turned["secret"]) == 200
