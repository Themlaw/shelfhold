"""PR #282 (M4 L46), relecture adverse : les trous réels des jetons d'appareil, en rouge.

Chaque test est un `xfail` strict : il passe au vert le jour où l'objection est levée, et il
faut alors retirer la marque (docs/decisions/M4.md, « #282 : contradicteur »).
"""

from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import LocalAuth
from tests.auth_env import PASSWORD, AuthEnv, auth_env
from tests.integration.test_l46_device_tokens import KOSYNC_AUTH, PER_ADDRESS, kosync

pytestmark = pytest.mark.req("EF-USR-008", "ENF-SEC-008")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _auth(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def _paired(env: AuthEnv) -> tuple[str, str]:
    _auth(env).profile_add("mum", PASSWORD, "member")
    created = _auth(env).create_device_token(subject_username="mum", name="liseuse")
    return created.public_id, created.secret


@pytest.mark.xfail(
    strict=True,
    reason="contradicteur #282 MAJ-1 : le plafond par adresse refuse aussi le bon secret",
)
def test_poc_guesses_from_a_shared_address_never_lock_out_the_right_key(api: AuthEnv) -> None:
    """Derrière un NAT, un CGNAT ou un proxy mal déclaré (`http_trusted_proxies` vide), toutes les
    liseuses partagent une adresse. 30 essais faux par minute, chacun sous un identifiant inventé,
    suffisent à tenir en 429 toutes les liseuses de cette adresse, indéfiniment. Avec 130 bits de
    secret, le plafond par adresse ne protège rien qu'il faille payer de ce déni de service :
    le plafond par couple suffit à borner un jeton précis."""
    public_id, secret = _paired(api)
    shared = api.other_client("198.51.100.30")
    for index in range(PER_ADDRESS):
        guess = f"{index:08d}".replace("0", "x")
        assert shared.get(KOSYNC_AUTH, headers=kosync(guess, "n" * 26)).status_code == 401
    assert shared.get(KOSYNC_AUTH, headers=kosync(public_id, secret)).status_code == 200


@pytest.mark.xfail(
    strict=True,
    reason="contradicteur #282 MAJ-2 : ADR-0053 §4 impose TLS, la route l'accepte en clair",
)
def test_poc_device_routes_refuse_plain_http_by_default(api: AuthEnv) -> None:
    """ADR-0053 §4 : « TLS obligatoire (HTTP simple seulement par réglage explicite de l'admin) ».
    En HTTP simple, le secret Basic et le md5 de kosync passent en clair et se rejouent tels quels.
    Aucun réglage ne l'autorise ici : la requête doit être refusée, pas servie."""
    public_id, secret = _paired(api)
    plain = api.other_client("192.0.2.40", base_url="http://testserver")
    assert plain.get(KOSYNC_AUTH, headers=kosync(public_id, secret)).status_code != 200
