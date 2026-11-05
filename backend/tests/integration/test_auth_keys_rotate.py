"""`admin keys rotate` (EF-USR-003, P4): the new key is shown once, the old one dies with it."""

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing

import pytest

from shelfhold.config import Settings
from tests.auth_env import AuthEnv, auth_env, db_dump, db_rows, run_cli

LIFETIME_DAYS = (
    "SELECT julianday(expires_at) - julianday(created_at) FROM api_key WHERE revoked_at IS NULL"
)
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    *argv: str,
) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run_cli(monkeypatch, settings, "admin", "keys", *argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _create(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    *extra: str,
) -> str:
    code, out, _ = _run(monkeypatch, capsys, settings, "create", "--name", "scripts", *extra)
    assert code == 0
    match = KEY.search(out)
    assert match
    return match.group(0)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _prefix(key: str) -> str:
    match = KEY.fullmatch(key)
    assert match
    return match.group(1)


@pytest.mark.req("EF-USR-003")
def test_cli_keys_rotate_prints_new_key_once_and_revokes_the_old_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    old = _create(monkeypatch, capsys, api.settings)
    code, out, err = _run(monkeypatch, capsys, api.settings, "rotate", _prefix(old))
    assert code == 0
    assert out.strip() == KEY.search(out).group(0)  # type: ignore[union-attr]  # stdout: the key alone
    new = out.strip()
    assert new != old
    assert new not in err  # the diagnostics never repeat the secret
    assert api.client.get("/api/v1/about", headers=_bearer(old)).status_code == 401
    assert api.client.get("/api/v1/about", headers=_bearer(new)).status_code == 200
    assert new.split("_", 2)[2] not in db_dump(api.settings)  # stored hashed
    code, listing, _ = _run(monkeypatch, capsys, api.settings, "list")
    assert code == 0
    assert _prefix(new) in listing
    assert new not in listing


@pytest.mark.req("EF-USR-003")
def test_rotate_keeps_owner_name_and_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    old = _create(monkeypatch, capsys, api.settings, "--read-only")
    _, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", _prefix(old))
    new = out.strip()
    rows = db_rows(
        api.settings,
        "SELECT prefix, user_id, name, scopes, read_only FROM api_key ORDER BY created_at",
    )
    (old_prefix, old_user, old_name, old_scopes, old_ro), (new_prefix, user, name, scopes, ro) = (
        rows
    )
    assert (old_prefix, new_prefix) == (_prefix(old), _prefix(new))
    assert (user, name, scopes, ro) == (old_user, old_name, old_scopes, old_ro)
    assert api.client.get("/api/v1/about", headers=_bearer(new)).status_code == 200


@pytest.mark.req("EF-USR-003")
def test_rotate_expiry_is_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    never = _create(monkeypatch, capsys, api.settings)
    _, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", _prefix(never))
    assert db_rows(api.settings, "SELECT expires_at FROM api_key WHERE revoked_at IS NULL") == [
        (None,)
    ]
    # An explicit lifetime applies to the new key only.
    _run(monkeypatch, capsys, api.settings, "rotate", _prefix(out.strip()), "--expires-days", "30")
    [(days,)] = db_rows(
        api.settings,
        LIFETIME_DAYS,
    )
    assert 29.9 < float(days) <= 30.01  # type: ignore[arg-type]
    # Without the option, a key that had a lifetime keeps the same lifetime.
    [(live,)] = db_rows(api.settings, "SELECT prefix FROM api_key WHERE revoked_at IS NULL")
    _run(monkeypatch, capsys, api.settings, "rotate", str(live))
    [(days,)] = db_rows(
        api.settings,
        LIFETIME_DAYS,
    )
    assert 29.9 < float(days) <= 30.01  # type: ignore[arg-type]


@pytest.mark.req("EF-USR-003")
def test_rotate_an_expired_key_is_allowed_a_revoked_or_unknown_one_is_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    expiring = _create(monkeypatch, capsys, api.settings, "--expires-days", "1")
    # The CLI reads the system clock: force the stored expiry into the past.
    with closing(sqlite3.connect(api.settings.database_path)) as connection, connection:
        connection.execute("UPDATE api_key SET expires_at = '2000-01-01 00:00:00'")
    code, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", _prefix(expiring))
    assert code == 0  # the whole point of a rotation after `target.key_expiring`
    revoked = _prefix(expiring)
    before = db_rows(api.settings, "SELECT count(*) FROM api_key")
    code, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", revoked)
    assert code != 0
    assert not KEY.search(out)
    code, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", "deadbeef")
    assert code != 0
    assert not KEY.search(out)
    assert db_rows(api.settings, "SELECT count(*) FROM api_key") == before


@pytest.mark.req("EF-USR-003")
def test_rotate_is_atomic_when_the_new_key_cannot_be_stored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    old = _create(monkeypatch, capsys, api.settings)
    from shelfhold.identity.domain import tokens

    real = tokens.new_api_key()
    # Same prefix as the old key: the unique index refuses the insert, every time it is drawn.
    clash = tokens.ApiKeySecret(prefix=_prefix(old), value=real.value)
    monkeypatch.setattr("shelfhold.identity.application.service.new_api_key", lambda: clash)
    code, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", _prefix(old))
    assert code != 0
    assert not KEY.search(out)  # nothing is shown when nothing was stored
    assert api.client.get("/api/v1/about", headers=_bearer(old)).status_code == 200  # still alive
    assert db_rows(api.settings, "SELECT count(*) FROM api_key WHERE revoked_at IS NOT NULL") == [
        (0,)
    ]


@pytest.mark.req("EF-USR-003")
def test_rotate_writes_one_audit_entry_and_never_the_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    old = _create(monkeypatch, capsys, api.settings)
    _, out, _ = _run(monkeypatch, capsys, api.settings, "rotate", _prefix(old))
    new = out.strip()
    # The server runs, so the CLI went through its socket: the actor is `local_cli:<uid>` and the
    # target needs no host uid (the offline form is covered in test_local_cli_commands).
    rows = db_rows(
        api.settings, "SELECT actor_id, action, target FROM audit WHERE action = 'api_key.rotated'"
    )
    assert rows == [
        (f"local_cli:{os.getuid()}", "api_key.rotated", f"{_prefix(old)}>{_prefix(new)}")
    ]
    actors = db_rows(api.settings, "SELECT actor_id FROM audit WHERE action LIKE 'api_key.%'")
    assert all(a == f"local_cli:{os.getuid()}" for (a,) in actors)  # created, rotated
    assert new not in db_dump(api.settings)
