"""M4 L46: device tokens, issued for a reader profile (ADR-0053 §4, ADR-0049, EF-USR-008,
EF-USR-022 (4), EF-USR-024, EF-LEC-009, ENF-SEC-008, ENF-SEC-010, ENF-SEC-013).

A token belongs to a reader profile and gives its rights and its filters, nothing else. It opens
the OPDS and the kosync routes only (no ordinary API route, no reserved action), it is generated
and shown once, only the SHA-256 of its `md5` is stored, a wrong guess is counted per (address,
identifier) and per address, and a revocation is immediate. The attacks are tried through the real
application: child token, member token, revoked token, another profile's token (IDOR), an API
key, brute force, and a token presented on an administration route.
"""

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request

from shelfhold.config import Settings
from shelfhold.identity.domain.device_secret import normalise_secret
from shelfhold.identity.domain.roles import ProfileError
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    Credential,
    CredentialNotAllowed,
    LocalAuth,
    NotAllowedError,
    Principal,
    ThrottledError,
    UnknownUserError,
    local_auth,
    reader_scope,
)
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.interfaces.http_api.device_auth import (
    DEVICE_ROUTE_PREFIXES,
    require_device_basic,
    require_device_kosync,
)
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import credential_label
from tests.auth_env import ADMIN, PASSWORD, AuthEnv, auth_env, db_dump, db_rows, run_cli
from tests.diagnostic_env import _views, trap_instance

pytestmark = pytest.mark.req("EF-USR-008", "EF-USR-024", "ENF-SEC-008")

KOSYNC_AUTH = "/kosync/users/auth"
OPDS_PATH = "/opds/series"
PER_PAIR = 5
PER_ADDRESS = 30
ADMIN_ROUTES = (
    "/api/v1/profiles",
    "/api/v1/maintenance",
    "/api/v1/destinations",
    "/api/v1/stats",
    "/api/v1/shelves",
)
OUTSIDE_PATH = "/api/v1/profiles"


def md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()  # KOReader's own key


def kosync(public_id: str, secret_or_key: str, *, hashed: bool = True) -> dict[str, str]:
    key = md5(secret_or_key) if hashed else secret_or_key
    return {
        "accept": "application/vnd.koreader.v1+json",
        "x-auth-user": public_id,
        "x-auth-key": key,
    }


def basic(user: str, secret: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@dataclass
class Answer:
    """What a dependency answered, in the shape of a response (`status_code`, `json()`)."""

    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]

    def json(self) -> dict[str, Any]:
        return self.body


def kosync_status(client: Any, created: Any, headers: dict[str, str] | None = None) -> int:
    """The status kosync answers `client` with the token's own key (or the given headers)."""
    sent = headers if headers is not None else kosync(created.public_id, created.secret)
    return int(client.get(KOSYNC_AUTH, headers=sent).status_code)


def device_get(
    env: AuthEnv,
    headers: dict[str, str] | None = None,
    *,
    path: str = OPDS_PATH,
    ip: str = "203.0.113.50",
    check: Callable[[Request], Principal] = require_device_basic,
) -> Answer:
    """The dependency of an OPDS or kosync route, called with a real request: no route is
    declared in a test, the dependency is what every such route will name (L47, L48)."""
    scope = {
        "type": "http", "method": "GET", "path": path, "query_string": b"", "scheme": "https",
        "server": ("testserver", 443), "client": (ip, 40000), "app": env.app,
        "state": {"client_ip": ip},
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }  # fmt: skip
    try:
        principal = check(Request(scope))
    except ProblemError as refused:
        return Answer(
            refused.status,
            {"code": str(refused), **refused.extensions},
            {k.lower(): v for k, v in refused.headers.items()},
        )
    return Answer(
        200,
        {
            "user": principal.user_id,
            "role": principal.role,
            "credential": principal.credential.value,
        },
        {},
    )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def auth_of(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def profile(env: AuthEnv, username: str, role: str, **extra: Any) -> str:
    return auth_of(env).profile_add(username, PASSWORD, role, **extra)


def user_id(env: AuthEnv, username: str) -> str:
    return str(db_rows(env.settings, 'SELECT id FROM "user" WHERE username = ?', username)[0][0])


def _same_problem(first: Any, second: Any) -> bool:
    """The same answer but for the request identifier, which is the request's own."""
    one, two = first.json(), second.json()
    one.pop("request_id", None), two.pop("request_id", None)
    return bool(one == two)


def issue(env: AuthEnv, subject: str, **kwargs: Any) -> Any:
    return auth_of(env).create_device_token(subject_username=subject, name="liseuse", **kwargs)


# --- the token is issued FOR a profile, and acts as that profile --------------------------------


def test_pairing_requires_explicit_subject_profile(api: AuthEnv) -> None:
    with pytest.raises(TypeError):
        auth_of(api).create_device_token(name="liseuse")  # type: ignore[call-arg]
    for missing in (None, "", "  "):
        with pytest.raises(ValueError, match="subject_required"):
            auth_of(api).create_device_token(subject_username=missing)  # type: ignore[arg-type]
    with pytest.raises(UnknownUserError):
        auth_of(api).create_device_token(subject_username="nobody")
    assert db_rows(api.settings, "SELECT count(*) FROM device_token") == [(0,)]


def test_device_token_scoped_to_subject_profile_not_issuer(api: AuthEnv) -> None:
    kid = profile(api, "kid", "child", max_age_rating=7)
    admin = user_id(api, ADMIN)
    session = api.login()
    assert session.status_code == 200
    admin_principal = Principal(
        user_id=admin, username=ADMIN, role="admin", credential=Credential.SESSION,
        session_id=None, auth_time=api.clock.now(), can_rate=True,
    )  # fmt: skip
    created = auth_of(api).create_device_token(
        subject_username="kid", name="Kobo", by=admin_principal
    )
    row = db_rows(api.settings, "SELECT subject_user_id, issued_by FROM device_token")[0]
    assert row == (kid, admin)  # the reader and, apart, who paired it
    principal = auth_of(api).authenticate_device_token(
        created.public_id, secret=created.secret, ip="203.0.113.9"
    )
    assert principal is not None
    assert (principal.user_id, principal.role) == (kid, "child")
    assert principal.credential is Credential.DEVICE_TOKEN
    assert principal.can_rate is False and principal.scopes == ()
    assert principal.user_id != admin


def test_a_member_cannot_issue_a_token_and_the_host_cli_names_no_issuer(api: AuthEnv) -> None:
    profile(api, "mum", "member")
    mum = Principal(
        user_id=user_id(api, "mum"), username="mum", role="member",
        credential=Credential.SESSION, session_id=None, auth_time=api.clock.now(),
    )  # fmt: skip
    with pytest.raises(NotAllowedError):
        auth_of(api).create_device_token(subject_username="mum", by=mum)
    issue(api, "mum")
    assert db_rows(api.settings, "SELECT issued_by FROM device_token") == [(None,)]


# --- the secret: generated, shown once, only the digest of its md5 is stored --------------------


def test_secret_is_shown_once_and_never_stored(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    dump = db_dump(api.settings)
    for forbidden in (created.secret, md5(created.secret)):
        assert forbidden not in dump
    digest = db_rows(api.settings, "SELECT secret_digest FROM device_token")[0][0]
    assert digest == "sha256:" + hashlib.sha256(md5(created.secret).encode()).hexdigest()
    assert created.secret not in repr(created) and created.secret not in str(created)
    listed = auth_of(api).device_tokens()
    assert len(listed) == 1 and created.secret not in repr(listed[0])
    assert digest not in repr(listed[0])  # a listing never carries what verifies a secret


def test_kosync_auth_uses_device_token_secret(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    ok = api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret))
    assert ok.status_code == 200 and ok.json() == {"authorized": "OK"}
    # the account password is never a secret here, as a key or as a Basic password
    wrong = api.client.get(KOSYNC_AUTH, headers=kosync("kid", PASSWORD))
    assert wrong.status_code == 401
    assert device_get(api, basic("kid", PASSWORD)).status_code == 401
    assert device_get(api, basic(created.public_id, PASSWORD)).status_code == 401
    # the md5 of the account password is no key either
    assert (
        api.client.get(
            KOSYNC_AUTH, headers=kosync(created.public_id, md5(PASSWORD), hashed=False)
        ).status_code
        == 401
    )


def test_one_token_opens_both_doors_basic_for_opds_and_md5_for_kosync(api: AuthEnv) -> None:
    kid = profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    typed = created.secret.upper()  # a Basic client may type capitals; kosync, the form shown
    for secret in (created.secret, typed):
        answer = device_get(api, basic(created.public_id, secret))
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"user": kid, "role": "child", "credential": "device_token"}
    # KOReader sends md5 of exactly what was typed: the canonical form only
    assert api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).is_success
    assert api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, typed)).status_code == 401


def test_identical_answer_for_an_unknown_identifier_and_a_wrong_key(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    unknown = api.client.get(KOSYNC_AUTH, headers=kosync("zzzzzzzz", created.secret))
    wrong = api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, "x" * 26))
    assert unknown.status_code == wrong.status_code == 401
    assert _same_problem(unknown, wrong)
    assert unknown.headers.get("www-authenticate") == wrong.headers.get("www-authenticate")
    basic_unknown = device_get(api, basic("zzzzzzzz", created.secret))
    basic_wrong = device_get(api, basic(created.public_id, "x" * 26))
    assert basic_unknown.status_code == basic_wrong.status_code == 401
    assert _same_problem(basic_unknown, basic_wrong)
    assert "basic" in basic_wrong.headers["www-authenticate"].lower()


def test_no_secret_in_logs_audit_or_errors(
    api: AuthEnv, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    wrong = "q" * 26
    api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret))
    api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, wrong))
    device_get(api, basic(created.public_id, wrong))
    auth_of(api).revoke_device_token(created.public_id)
    seen = capsys.readouterr()
    everything = (
        seen.out + seen.err + caplog.text + db_dump(api.settings) + repr(created)
    )  # audit lines are in the dump
    secrets_ = (created.secret, md5(created.secret), wrong, md5(wrong), base64.b64encode(
        f"{created.public_id}:{wrong}".encode()
    ).decode())  # fmt: skip
    for secret in secrets_:
        assert secret not in everything
    audited = db_rows(
        api.settings, "SELECT action, target FROM audit WHERE action LIKE 'device_token.%'"
    )
    assert [a for a, _ in audited] == ["device_token.created", "device_token.revoked"]
    assert all(created.public_id in (t or "") for _a, t in audited)


# --- lifecycle: immediate revocation, expiry, use ----------------------------------------------


def test_revoked_token_is_refused_at_once_and_stays_refused(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    headers = kosync(created.public_id, created.secret)
    assert api.client.get(KOSYNC_AUTH, headers=headers).status_code == 200
    assert auth_of(api).revoke_device_token(created.public_id) is True
    assert api.client.get(KOSYNC_AUTH, headers=headers).status_code == 401  # no cache
    assert device_get(api, basic(created.public_id, created.secret)).status_code == 401
    assert auth_of(api).revoke_device_token(created.public_id) is False  # already revoked
    assert auth_of(api).revoke_device_token("zzzzzzzz") is False
    revoked_at = db_rows(api.settings, "SELECT revoked_at FROM device_token")[0][0]
    assert revoked_at is not None


def test_revoking_one_token_leaves_the_others_of_the_same_profile(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    salon, bedroom = issue(api, "kid"), issue(api, "kid")
    auth_of(api).revoke_device_token(salon.public_id)
    assert (
        api.client.get(KOSYNC_AUTH, headers=kosync(salon.public_id, salon.secret)).status_code
        == 401
    )
    assert (
        api.client.get(KOSYNC_AUTH, headers=kosync(bedroom.public_id, bedroom.secret)).status_code
        == 200
    )


def test_expired_token_is_refused_and_use_is_recorded(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid", expires_days=2)
    assert db_rows(api.settings, "SELECT last_used_at FROM device_token") == [(None,)]
    headers = kosync(created.public_id, created.secret)
    assert api.client.get(KOSYNC_AUTH, headers=headers).status_code == 200
    assert db_rows(api.settings, "SELECT last_used_at FROM device_token")[0][0] is not None
    api.clock.advance(timedelta(days=3))
    assert api.client.get(KOSYNC_AUTH, headers=headers).status_code == 401
    with pytest.raises(ValueError, match="invalid_expiry"):
        issue(api, "kid", expires_days=0)


def test_password_reset_of_the_profile_closes_its_device_tokens(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    profile(api, "mum", "member")
    kid, mum = issue(api, "kid"), issue(api, "mum")
    auth_of(api).reset_password("kid", "another long password")  # a lost e-reader is the reason
    assert api.client.get(KOSYNC_AUTH, headers=kosync(kid.public_id, kid.secret)).status_code == 401
    assert api.client.get(KOSYNC_AUTH, headers=kosync(mum.public_id, mum.secret)).status_code == 200


def test_deleting_the_profile_deletes_its_tokens(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    auth_of(api).delete_user("kid")
    assert db_rows(api.settings, "SELECT count(*) FROM device_token") == [(0,)]
    assert (
        api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).status_code
        == 401
    )


# --- a token gives the rights and the filters of its profile, read at each request --------------


def test_the_ceiling_and_the_role_are_read_at_each_request(api: AuthEnv) -> None:
    kid = profile(api, "kid", "child", max_age_rating=12)
    created = issue(api, "kid")
    engine = create_db_engine(api.settings.database_path)
    try:
        principal = auth_of(api).authenticate_device_token(
            created.public_id, secret=created.secret, ip="203.0.113.9"
        )
        assert principal is not None
        scope = reader_scope(engine, principal.user_id)
        assert (scope.kind, scope.ceiling) == ("ceiling", 12) and not scope.unrestricted
        auth_of(api).profile_set(kid, max_age_rating=3)  # lowered: the next request sees it
        again = auth_of(api).authenticate_device_token(
            created.public_id, secret=created.secret, ip="203.0.113.9"
        )
        assert again is not None
        assert reader_scope(engine, again.user_id).ceiling == 3
        assert reader_scope(engine, user_id(api, ADMIN)).unrestricted  # not the issuer's scope
    finally:
        engine.dispose()


@pytest.mark.parametrize("role", ["child", "member"])
def test_a_device_token_is_refused_every_reserved_action_whatever_its_profile(
    api: AuthEnv, role: str
) -> None:
    profile(api, "reader", role, **({"max_age_rating": 7} if role == "child" else {}))
    created = issue(api, "reader")
    principal = auth_of(api).authenticate_device_token(
        created.public_id, secret=created.secret, ip="203.0.113.9"
    )
    assert principal is not None and principal.credential is Credential.DEVICE_TOKEN
    assert RESERVED_ACTIONS
    for action in RESERVED_ACTIONS.values():
        verdict = auth_of(api).authorize(
            principal,
            action,
            restricted_profile_exists=True,
            allowed_roles=frozenset({"admin", "member", "child"}),
            audit=False,
        )
        assert isinstance(verdict, CredentialNotAllowed), action.name
        assert not isinstance(verdict, Allowed)


# --- the reach is closed: OPDS and kosync, never an administration or an ordinary route -------


@pytest.mark.parametrize("role", ["child", "member"])
def test_device_token_presented_on_admin_and_ordinary_routes_is_refused(
    api: AuthEnv, role: str
) -> None:
    profile(api, "reader", role, **({"max_age_rating": 7} if role == "child" else {}))
    created = issue(api, "reader")
    login = api.login()
    assert login.status_code == 200
    for path in ADMIN_ROUTES:  # the routes exist: the admin's own session is not told "absent"
        assert api.client.get(path).status_code != 404, path
    api.client.cookies.clear()
    spoken = (
        kosync(created.public_id, created.secret),
        basic(created.public_id, created.secret),
        {"Authorization": f"Bearer {created.secret}"},
        {"Authorization": f"Bearer {created.public_id}"},
        {**kosync(created.public_id, created.secret), **basic(created.public_id, created.secret)},
    )
    for headers in spoken:
        # a wrong bearer is a failed key attempt, counted (ENF-SEC-008): a few requests only
        bearer = "Authorization" in headers and headers["Authorization"].startswith("Bearer")
        for path in ADMIN_ROUTES[:2] if bearer else ADMIN_ROUTES:
            assert api.client.get(path, headers=headers).status_code == 401, (path, headers)
        for method in ("post",) if bearer else ("post", "put", "delete", "patch"):
            answer = getattr(api.client, method)("/api/v1/profiles", headers=headers)
            assert answer.status_code in {401, 403, 405}, (method, headers)
    assert db_rows(api.settings, 'SELECT count(*) FROM "user"')[0][0] == 2


def test_a_route_outside_opds_and_kosync_never_takes_a_device_token(api: AuthEnv) -> None:
    """The closed list is enforced where the token is read, not only by where routes happen to
    be mounted: a route that names the dependency outside `/opds` and `/kosync` refuses it."""
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    for check, headers in (
        (require_device_kosync, kosync(created.public_id, created.secret)),
        (require_device_basic, basic(created.public_id, created.secret)),
    ):
        answer = device_get(api, headers, path=OUTSIDE_PATH, check=check)
        assert answer.status_code == 403 and answer.json()["code"] == "credential_not_allowed"
    assert api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).is_success
    assert set(DEVICE_ROUTE_PREFIXES) == {"/opds", "/kosync"}


def test_an_api_key_is_no_device_token_and_a_device_token_is_no_api_key(api: AuthEnv) -> None:
    kid = profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    key = auth_of(api).create_api_key(name="k", scope="admin").value
    assert api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, key)).status_code == 401
    assert device_get(api, {"Authorization": f"Bearer {key}"}).status_code == 401
    assert device_get(api, basic(created.public_id, key)).status_code == 401
    assert (
        api.client.get("/api/v1/stats", headers={"Authorization": f"Bearer {key}"}).status_code
        != 401
    )
    assert user_id(api, "kid") == kid


def test_a_session_cookie_does_not_open_the_device_routes(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    assert api.client.get(KOSYNC_AUTH).status_code == 401
    assert device_get(api).status_code == 401


# --- IDOR: another profile's token -------------------------------------------------------------


def test_another_profiles_token_never_opens_a_door_as_someone_else(api: AuthEnv) -> None:
    kid = profile(api, "kid", "child", max_age_rating=7)
    mum = profile(api, "mum", "member")
    kid_token, mum_token = issue(api, "kid"), issue(api, "mum")
    # crossed: the identifier of one with the secret of the other
    assert (
        api.client.get(
            KOSYNC_AUTH, headers=kosync(kid_token.public_id, mum_token.secret)
        ).status_code
        == 401
    )
    assert device_get(api, basic(mum_token.public_id, kid_token.secret)).status_code == 401
    # each token is exactly its own profile, whatever else is asked
    one = device_get(api, basic(kid_token.public_id, kid_token.secret)).json()
    two = device_get(api, basic(mum_token.public_id, mum_token.secret)).json()
    assert (one["user"], two["user"]) == (kid, mum)
    # the identifier is the profile's username: it never names a profile
    assert api.client.get(KOSYNC_AUTH, headers=kosync("kid", kid_token.secret)).status_code == 401
    assert api.client.get(KOSYNC_AUTH, headers=kosync("admin", kid_token.secret)).status_code == 401


# --- brute force --------------------------------------------------------------------------------


def test_five_wrong_keys_per_address_and_identifier_then_even_the_right_one_waits(
    api: AuthEnv,
) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    attacker = api.other_client("198.51.100.7")
    for _ in range(PER_PAIR):
        assert (
            attacker.get(KOSYNC_AUTH, headers=kosync(created.public_id, "n" * 26)).status_code
            == 401
        )
    blocked = attacker.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret))
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    # the reader's own address is another pair: not slowed down by someone else's guesses
    reader = api.other_client("192.0.2.20")
    assert (
        reader.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).status_code
        == 200
    )
    api.clock.advance(timedelta(seconds=61))
    assert (
        attacker.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).status_code
        == 200
    )


def test_thirty_wrong_attempts_per_address_whatever_the_identifier(api: AuthEnv) -> None:
    """The address limit slows failures only (MAJ-1 of the adversarial review): past it a wrong
    guess is a 429, a right secret is still served."""
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    attacker = api.other_client("198.51.100.8")
    for index in range(PER_ADDRESS):  # a new identifier each time: no pair ever reaches five
        answer = attacker.get(
            KOSYNC_AUTH, headers=kosync(f"{index:08d}".replace("0", "2"), "n" * 26)
        )
        assert answer.status_code == 401
    assert attacker.get(KOSYNC_AUTH, headers=kosync("23232323", "n" * 26)).status_code == 429
    assert device_get(api, basic("23232323", "n" * 26), ip="198.51.100.8").status_code == 429
    other = api.other_client("192.0.2.21")
    assert (
        other.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).status_code == 200
    )


def test_the_right_reader_passes_after_thirty_failures_under_invented_identifiers(
    api: AuthEnv,
) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    shared = api.other_client("198.51.100.30")
    for index in range(PER_ADDRESS):
        guess = f"{index:08d}".replace("0", "x")
        assert shared.get(KOSYNC_AUTH, headers=kosync(guess, "n" * 26)).status_code == 401
    assert shared.get(KOSYNC_AUTH, headers=kosync("23232323", "n" * 26)).status_code == 429
    assert kosync_status(shared, created) == 200
    opds = device_get(api, basic(created.public_id, created.secret), ip="198.51.100.30")
    assert opds.status_code == 200
    # the pair limit stays: a reader's own identifier guessed wrongly five times waits, even right
    for _ in range(PER_PAIR):
        shared.get(KOSYNC_AUTH, headers=kosync(created.public_id, "n" * 26))
    assert kosync_status(shared, created) == 429


def test_a_good_token_does_not_pay_for_its_own_successes(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    reader = api.other_client("192.0.2.22")
    for _ in range(PER_ADDRESS + PER_PAIR + 5):  # a sync of a whole library is many requests
        assert (
            reader.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).status_code
            == 200
        )
    # a success clears the pair: four typos, a right key, four more typos, still no wait
    for _ in range(PER_PAIR - 1):
        reader.get(KOSYNC_AUTH, headers=kosync(created.public_id, "n" * 26))
    assert (
        reader.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret)).status_code
        == 200
    )
    for _ in range(PER_PAIR - 1):
        assert (
            reader.get(KOSYNC_AUTH, headers=kosync(created.public_id, "n" * 26)).status_code == 401
        )


def test_the_service_raises_a_throttle_error_with_the_wait(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    for _ in range(PER_PAIR):
        assert (
            auth_of(api).authenticate_device_token(
                created.public_id, secret="n" * 26, ip="198.51.100.9"
            )
            is None
        )
    with pytest.raises(ThrottledError) as refused:
        auth_of(api).authenticate_device_token(
            created.public_id, secret=created.secret, ip="198.51.100.9"
        )
    assert refused.value.retry_after_s >= 1


def test_an_accepted_token_buys_its_own_budget_a_guess_does_not(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    known = api.app.state.recognised_devices  # type: ignore[attr-defined]
    guess = kosync(created.public_id, "n" * 26)
    api.client.get(KOSYNC_AUTH, headers=guess)
    assert credential_label(guess["x-auth-key"]) not in known
    good = kosync(created.public_id, created.secret)
    api.client.get(KOSYNC_AUTH, headers=good)
    assert credential_label(good["x-auth-key"]) in known


# --- the host CLI ------------------------------------------------------------------------------


def test_cli_issues_lists_and_revokes_without_ever_listing_the_secret(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with auth_env(monkeypatch, settings) as api:
        profile(api, "kid", "child", max_age_rating=7)
        capsys.readouterr()
    assert (
        run_cli(
            monkeypatch,
            settings,
            "admin",
            "device-tokens",
            "create",
            "--profile",
            "kid",
            "--name",
            "Salon",
        )
        == 0
    )
    out, err = capsys.readouterr()
    typed = out.strip()
    assert typed.count("\n") == 0 and len(normalise_secret(typed)) == 26  # stdout: the secret alone
    assert "kid" in err
    public_id = db_rows(settings, "SELECT public_id FROM device_token")[0][0]
    assert public_id in err
    assert run_cli(monkeypatch, settings, "admin", "device-tokens", "list", "--json") == 0
    listing = capsys.readouterr().out
    rows = json.loads(listing)
    assert rows[0]["id"] == public_id and rows[0]["profile"] == "kid" and rows[0]["name"] == "Salon"
    assert rows[0]["revoked"] is False and "last_used_at" in rows[0]
    assert normalise_secret(typed) not in listing and "sha256" not in listing
    assert db_rows(settings, "SELECT issued_by FROM device_token") == [(None,)]
    assert run_cli(monkeypatch, settings, "admin", "device-tokens", "revoke", str(public_id)) == 0
    assert db_rows(settings, "SELECT revoked_at IS NOT NULL FROM device_token") == [(1,)]
    assert run_cli(monkeypatch, settings, "admin", "device-tokens", "revoke", str(public_id)) != 0


def test_cli_requires_a_profile_and_knows_it(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with auth_env(monkeypatch, settings) as _api:
        pass
    with pytest.raises(SystemExit) as parsed:
        run_cli(monkeypatch, settings, "admin", "device-tokens", "create", "--name", "Salon")
    assert parsed.value.code == 2  # no default profile: the pairing asks for it
    assert (
        run_cli(monkeypatch, settings, "admin", "device-tokens", "create", "--profile", "ghost")
        != 0
    )
    assert db_rows(settings, "SELECT count(*) FROM device_token") == [(0,)]
    capsys.readouterr()


def test_the_database_holds_no_token_secret_after_a_whole_life(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    api.client.get(KOSYNC_AUTH, headers=kosync(created.public_id, created.secret))
    auth_of(api).revoke_device_token(created.public_id)
    connection = sqlite3.connect(api.settings.database_path)
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert created.secret not in dump and md5(created.secret) not in dump


def test_the_diagnostic_bundle_holds_no_device_token_secret_md5_or_digest(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    trap_instance(monkeypatch, settings, tmp_path)
    engine = create_db_engine(settings.database_path)
    try:
        auth = local_auth(engine, settings)
        auth.profile_add("reader", PASSWORD, "member")  # an admin carries no token
        created = auth.create_device_token(subject_username="reader", name="liseuse")
    finally:
        engine.dispose()
    out = tmp_path / "out" / "bundle.zip"
    out.parent.mkdir()
    assert run_cli(monkeypatch, settings, "diagnostic", "bundle", "--out", str(out)) == 0
    digest = db_rows(settings, "SELECT secret_digest FROM device_token")[0][0]
    needles = {
        created.secret,
        created.secret,
        md5(created.secret),
        str(digest),
        str(digest).removeprefix("sha256:"),
    }
    for view in _views(out.read_bytes()):
        for needle in needles:
            assert needle not in view


# --- TLS is required unless the admin says otherwise (ADR-0053 §4, MAJ-2) ----------------------


def _scheme_scope(env: AuthEnv, headers: dict[str, str], *, scheme: str, peer: str) -> Answer:
    scope = {
        "type": "http", "method": "GET", "path": KOSYNC_AUTH, "query_string": b"",
        "scheme": scheme, "server": ("testserver", 80), "client": (peer, 40000), "app": env.app,
        "state": {"client_ip": peer},
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }  # fmt: skip
    try:
        principal = require_device_kosync(Request(scope))
    except ProblemError as refused:
        return Answer(refused.status, {"code": str(refused)}, {})
    return Answer(200, {"user": principal.user_id}, {})


def test_plain_http_is_refused_before_the_secret_is_checked(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    plain = api.other_client("192.0.2.40", base_url="http://testserver")
    for headers in (kosync(created.public_id, created.secret), kosync(created.public_id, "n" * 26)):
        answer = plain.get(KOSYNC_AUTH, headers=headers)
        assert answer.status_code == 403
        assert answer.json()["code"] == "device_tls_required"  # the same, right key or wrong
    answer = plain.get("/opds", headers=basic(created.public_id, created.secret))
    assert answer.status_code != 200
    # nothing was counted against the reader: the secret was never looked at
    secure = api.other_client("192.0.2.40")
    assert kosync_status(secure, created) == 200
    opds = device_get(api, basic(created.public_id, created.secret), ip="192.0.2.40")
    assert opds.status_code == 200


def test_plain_http_is_allowed_by_the_explicit_setting_only(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    assert Settings.model_fields["device_allow_plain_http"].default is False
    allowed = settings.model_copy(update={"device_allow_plain_http": True})
    with auth_env(monkeypatch, allowed) as env:
        profile(env, "kid", "child", max_age_rating=7)
        created = issue(env, "kid")
        plain = env.other_client("192.0.2.41", base_url="http://testserver")
        assert kosync_status(plain, created) == 200


def test_the_forwarded_scheme_is_ignored_without_a_trusted_proxy(api: AuthEnv) -> None:
    profile(api, "kid", "child", max_age_rating=7)
    created = issue(api, "kid")
    plain = api.other_client("192.0.2.42", base_url="http://testserver")
    headers = {**kosync(created.public_id, created.secret), "X-Forwarded-Proto": "https"}
    assert kosync_status(plain, created, headers) == 403


def test_the_forwarded_scheme_counts_behind_a_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    trusted = settings.model_copy(update={"http_trusted_proxies": ("192.0.2.0/24",)})
    with auth_env(monkeypatch, trusted) as env:
        profile(env, "kid", "child", max_age_rating=7)
        created = issue(env, "kid")
        headers = kosync(created.public_id, created.secret)
        proxy = env.other_client("192.0.2.43", base_url="http://testserver")
        assert kosync_status(proxy, created, {**headers, "X-Forwarded-Proto": "https"}) == 200
        said_http = {**headers, "X-Forwarded-Proto": "http"}
        assert kosync_status(proxy, created, said_http) == 403
        assert kosync_status(proxy, created, headers) == 403  # a proxy that says nothing
        stranger = env.other_client("198.51.100.44", base_url="http://testserver")  # not a proxy
        assert kosync_status(stranger, created, {**headers, "X-Forwarded-Proto": "https"}) == 403


# --- an admin profile carries no device token; a change of role closes the tokens ---------------


def test_an_admin_profile_cannot_be_the_subject_of_a_token(api: AuthEnv) -> None:
    with pytest.raises(ProfileError) as refused:
        issue(api, ADMIN)
    assert refused.value.code == "device_subject_is_admin"
    assert db_rows(api.settings, "SELECT count(*) FROM device_token")[0][0] == 0


def test_a_change_of_role_closes_the_device_tokens_of_the_profile_and_audits_it(
    api: AuthEnv,
) -> None:
    kid = profile(api, "kid", "child", max_age_rating=7)
    other = profile(api, "mum", "member")
    mine, theirs = issue(api, "kid"), issue(api, "mum")
    auth_of(api).profile_set(kid, role="member", max_age_rating=None)
    assert device_get(api, basic(mine.public_id, mine.secret)).status_code == 401
    assert device_get(api, basic(theirs.public_id, theirs.secret)).status_code == 200
    assert (
        other
        and db_rows(api.settings, "SELECT count(*) FROM device_token WHERE revoked_at IS NOT NULL")[
            0
        ][0]
        == 1
    )
    audited = db_rows(
        api.settings, "SELECT action, target FROM audit WHERE action LIKE 'device_token.%'"
    )
    assert any("device_tokens=1" in str(row[1]) for row in audited), audited
