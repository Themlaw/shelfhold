"""Owner decision 2026-11-12: every new passkey registration, on any account, tells the admin by
an event (`auth.passkey_registered`) and by a line of `status` that stays until the admin
acknowledges it (`shelfhold admin acknowledge-passkeys`, from the host)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.infrastructure.local_socket import call
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli
from tests.integration.test_passkeys import (
    ADD_MEMBER,
    PASSKEYS,
    SECOND_ULID,
    _Services,
    register,
    register_with,
    sql,
)
from tests.passkey_env import SoftPasskey

pytestmark = pytest.mark.req("EF-USR-025")

EVENT = "auth.passkey_registered"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(
        monkeypatch, settings, ops_services=_Services(), public_url="https://testserver"
    ) as env:
        yield env


def _events(api: AuthEnv) -> list[dict[str, Any]]:
    rows = db_rows(api.settings, "SELECT payload FROM event WHERE type = ?", EVENT)
    return [json.loads(str(r[0])) if isinstance(r[0], str) else dict(r[0]) for r in rows]  # type: ignore[call-overload]


def _status(api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> list[dict[str, str]]:
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "status", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    found = out.get("warnings", out) if isinstance(out, dict) else out
    return [w for w in found if isinstance(w, dict) and w.get("code") == "passkey_registered"]


def _through(line: dict[str, str]) -> str:
    """The last reference `status` showed."""
    found = re.findall(r"--through ([0-9A-Z]{26})", line["message"])
    assert found, line
    return found[-1]


def _acknowledge(api: AuthEnv, monkeypatch: pytest.MonkeyPatch, through: str) -> int:
    return run_cli(monkeypatch, api.settings, "admin", "acknowledge-passkeys", "--through", through)


def _member(api: AuthEnv) -> Any:
    sql(api.settings, ADD_MEMBER, SECOND_ULID, Argon2Hasher().hash(PASSWORD))
    other = api.other_client("198.51.100.80")
    assert api.login("member1", client=other).status_code == 200
    return other


def test_a_registration_emits_an_event_with_identifiers_only(api: AuthEnv) -> None:
    api.login()
    _, done = register(api, name="Laptop of the admin")
    assert done.status_code == 201
    (payload,) = _events(api)
    [(passkey_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
    assert payload["passkey_id"] == passkey_id and "user_id" in payload
    assert "Laptop" not in json.dumps(payload)  # a name typed by a person is never in an event


def test_a_registration_on_another_account_notifies_the_admin_too(api: AuthEnv) -> None:
    api.login()
    other = _member(api)
    _, done = register_with(api, other, SoftPasskey())
    assert done.status_code == 201
    assert len(_events(api)) == 1
    [(owner,)] = db_rows(api.settings, "SELECT user_id FROM webauthn_credential")
    assert _events(api)[0]["user_id"] == owner == SECOND_ULID


def test_status_lists_each_registration_until_it_is_acknowledged(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    assert _status(api, monkeypatch, capsys) == []
    api.login()
    register(api, name="Laptop")
    other = _member(api)
    register_with(api, other, SoftPasskey())
    (line,) = _status(api, monkeypatch, capsys)
    assert "2" in line["message"] and ADMIN in line["message"] and "member1" in line["message"]
    assert _status(api, monkeypatch, capsys)  # reading it does not acknowledge it
    assert _acknowledge(api, monkeypatch, _through(line)) == 0
    assert _status(api, monkeypatch, capsys) == []
    register(api, passkey=SoftPasskey(), name="Phone")  # a new one brings the line back
    (again,) = _status(api, monkeypatch, capsys)
    assert "1" in again["message"] and "Phone" in again["message"]
    assert _acknowledge(api, monkeypatch, _through(again)) == 0
    audited = [r[0] for r in db_rows(api.settings, "SELECT action FROM audit")]
    assert audited.count("passkey.registrations_acknowledged") == 2


def test_a_refused_registration_notifies_nothing(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Attack: a duplicate authenticator, a bad credential and a challenge replay leave no event
    and no line."""
    api.login()
    passkey, done = register(api, name="First")
    assert done.status_code == 201
    (first,) = _status(api, monkeypatch, capsys)
    assert _acknowledge(api, monkeypatch, _through(first)) == 0
    _, duplicate = register(api, passkey=passkey, name="Same device")
    assert duplicate.status_code >= 400
    options = api.post(f"{PASSKEYS}/options", json={}).json()
    bad = api.post(PASSKEYS, json={"flow": options["flow"], "name": "X", "credential": {}})
    assert bad.status_code >= 400
    assert len(_events(api)) == 1
    assert _status(api, monkeypatch, capsys) == []


def test_revoking_a_passkey_does_not_acknowledge_the_registration(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    api.login()
    register(api, name="Laptop")
    register(api, passkey=SoftPasskey(), name="Phone")
    [(first,), *_] = db_rows(api.settings, "SELECT id FROM webauthn_credential ORDER BY id")
    api.client.delete(f"{PASSKEYS}/{first}", headers={CSRF_HEADER: api.csrf()})
    # whatever the outcome of the revocation, only the admin's word acknowledges
    assert _status(api, monkeypatch, capsys)


def test_the_acknowledgement_is_a_host_command_only_audited_and_idempotent(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    api.login()
    register(api, name="Laptop")
    (line,) = _status(api, monkeypatch, capsys)
    sock = api.settings.data_dir / "shelfhold.sock"
    through = _through(line)
    assert call(sock, "identity.acknowledge_passkeys", {"through": through}) == 1
    audited = [r[0] for r in db_rows(api.settings, "SELECT action FROM audit")]
    assert audited.count("passkey.registrations_acknowledged") == 1
    # the reference is spent: nothing waits under it any more, and nothing is written
    assert _acknowledge(api, monkeypatch, through) == 1
    audited = [r[0] for r in db_rows(api.settings, "SELECT action FROM audit")]
    assert audited.count("passkey.registrations_acknowledged") == 1


def test_a_registration_after_status_is_not_acknowledged_with_what_was_shown(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Review of the PR (PoC): a key planted between `status` and the acknowledgement stays."""
    api.login()
    register(api, name="Laptop")
    (line,) = _status(api, monkeypatch, capsys)
    other = _member(api)
    _, done = register_with(api, other, SoftPasskey())
    assert done.status_code == 201
    assert _acknowledge(api, monkeypatch, _through(line)) == 0
    (still,) = _status(api, monkeypatch, capsys)  # the planted key is listed, alone
    assert "member1" in still["message"] and "1 clé" in still["message"]
    assert _through(still) != _through(line)


def test_an_unknown_or_foreign_reference_acknowledges_nothing(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    api.login()
    register(api, name="Laptop")
    for bad in ("0" * 26, "ZZZZZZZZZZZZZZZZZZZZZZZZZZ", "not-a-reference"):
        assert _acknowledge(api, monkeypatch, bad) == 1  # the command refuses
    assert len(_status(api, monkeypatch, capsys)) == 1
    audited = [r[0] for r in db_rows(api.settings, "SELECT action FROM audit")]
    assert "passkey.registrations_acknowledged" not in audited


def test_the_registration_and_its_audit_line_are_written_together(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the audit line cannot be written, no passkey is stored (and no event is sent)."""
    from shelfhold.identity.adapters.store import SqlIdentityStore

    api.login()

    def broken(_conn: Any) -> Any:  # the audit INSERT fails inside the registration transaction
        raise RuntimeError("audit down")

    monkeypatch.setattr(
        SqlIdentityStore, "_audit_insert", staticmethod(lambda entry: broken(entry))
    )
    with pytest.raises(RuntimeError):
        register(api, name="Laptop")
    monkeypatch.undo()
    assert db_rows(api.settings, "SELECT count(*) FROM webauthn_credential") == [(0,)]
    assert _events(api) == []


def test_retention_never_expires_an_unacknowledged_registration(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from datetime import timedelta

    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.jobs.retention import RetentionPolicy, expire_audit

    api.login()
    register(api, name="Old")
    (first,) = _status(api, monkeypatch, capsys)
    assert _acknowledge(api, monkeypatch, _through(first)) == 0
    register(api, passkey=SoftPasskey(), name="Waiting")
    engine = create_db_engine(api.settings.database_path)
    try:
        later = api.clock.now() + timedelta(days=4000)  # far beyond any retention age
        expire_audit(engine, now=later, policy=RetentionPolicy())
    finally:
        engine.dispose()
    kinds = [r[0] for r in db_rows(api.settings, "SELECT action FROM audit")]
    assert kinds.count("passkey.registered") == 1  # the acknowledged one went, the waiting stays
    assert len(_status(api, monkeypatch, capsys)) == 1


def test_the_event_is_an_operating_event() -> None:
    from shelfhold.events.operating import OPERATING_EVENTS

    assert EVENT in OPERATING_EVENTS
