"""PR #282 (M4 L46), adversarial review: the real gaps of the device tokens, now closed.

Both major objections (MAJ-1, MAJ-2) are lifted and their proofs run green
(docs/decisions/M4.md, "#282 : contradicteur").
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


def test_poc_guesses_from_a_shared_address_never_lock_out_the_right_key(api: AuthEnv) -> None:
    """Behind a NAT, a CGNAT or a badly declared proxy (empty `http_trusted_proxies`) every e-reader
    shares one address. Thirty wrong tries a minute, each under an invented identifier, used to
    keep all of them at 429. With 130 bits of secret the address limit buys nothing worth that
    denial of service: the pair limit bounds one token, and a right secret always passes."""
    public_id, secret = _paired(api)
    shared = api.other_client("198.51.100.30")
    for index in range(PER_ADDRESS):
        guess = f"{index:08d}".replace("0", "x")
        assert shared.get(KOSYNC_AUTH, headers=kosync(guess, "n" * 26)).status_code == 401
    assert shared.get(KOSYNC_AUTH, headers=kosync(public_id, secret)).status_code == 200


def test_poc_device_routes_refuse_plain_http_by_default(api: AuthEnv) -> None:
    """ADR-0053 §4: TLS is required, plain HTTP only by an explicit admin setting. In clear the
    Basic secret and the kosync md5 would travel and replay as they are: the request is refused,
    not served."""
    public_id, secret = _paired(api)
    plain = api.other_client("192.0.2.40", base_url="http://testserver")
    assert plain.get(KOSYNC_AUTH, headers=kosync(public_id, secret)).status_code != 200
