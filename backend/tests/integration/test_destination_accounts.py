"""M4 L12: the Kavita account of each member (EF-USR-012, D8, EF-USR-024).

Each member pastes their own Kavita key; Shelfhold seals it per user and per destination, checks
that it opens the account they declared, and never shows it again: not in the API, the logs, the
audit, the diagnostic bundle or the full export. A parent enters a child's key as a reserved
action; the admin can revoke a key, never read one; an API key links nothing. Every key below is
drawn at random by the simulated Kavita: no secret is a literal in a test.
"""

import io
import re
import sqlite3
import zipfile
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog

from shelfhold.config import Settings
from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import ADMIN, run_cli
from tests.destination_account_env import (
    ULID_UNKNOWN,
    Fixture,
    accounts_env,
    accounts_path,
)
from tests.diagnostic_env import _needles, _views
from tests.integration.test_schema_migration_0046 import add_row

PROFILES_PATH = "/api/v1/profiles"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
PRINCIPAL_KEYS = ("read", "request", "admin")


@pytest.fixture
def fx(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Fixture]:
    with accounts_env(monkeypatch, settings) as env:
        yield env


def _member(fx: Fixture, username: str, role: str = "member", **kwargs: Any) -> tuple[str, str]:
    """A profile, and the key of the Kavita account of the same name (the account exists there)."""
    user_id = fx.add(username, role, **kwargs)
    return user_id, fx.fake.add_member_key(f"{username}.kv")


def _link(fx: Fixture, username: str, token: str, destination: str | None = None) -> str:
    fx.as_user(username)
    done = fx.put(fx.users[username], destination or fx.destination_id, token, f"{username}.kv")
    assert done.status_code == 200, done.text
    return str(done.json()["token_fingerprint"])


def _everything_written(fx: Fixture) -> str:
    """The text of every place the application writes: the database file, its audit lines."""
    raw = Path(fx.settings.database_path).read_bytes().decode("latin-1")
    return raw + repr(fx.audit("%"))


# --- a token per user and per destination, sealed ------------------------------


@pytest.mark.req("EF-USR-012")
def test_kavita_account_mapping_token_per_user_encrypted(fx: Fixture) -> None:
    mum, mum_key = _member(fx, "mum")
    dad, dad_key = _member(fx, "dad")
    fingerprint = _link(fx, "mum", mum_key)
    _link(fx, "dad", dad_key)
    _link(fx, "mum", mum_key, fx.other_destination_id)  # a second destination: a second row
    rows = fx.rows(
        "SELECT id, user_id, shelf_destination_id, secret_ct, secret_nonce, token_fingerprint, "
        "verified_at FROM destination_account ORDER BY created_at, id"
    )
    assert [(r[1], r[2]) for r in rows] == [
        (mum, fx.destination_id),
        (dad, fx.destination_id),
        (mum, fx.other_destination_id),
    ]
    for _row_id, _u, _d, ciphertext, nonce, shown, verified in rows:
        assert ciphertext and nonce and shown and verified
        for key in (mum_key, dad_key):
            assert key.encode() not in bytes(ciphertext)  # type: ignore[arg-type]
    assert len({bytes(r[3]) for r in rows}) == 3  # type: ignore[arg-type]  # not one sealed value
    # sealed to its row: it opens there, with the keyring, and only there
    with closing(sqlite3.connect(fx.settings.database_path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM destination_account WHERE user_id = ?", (mum,)).fetchone()
    engine = create_db_engine(fx.settings.database_path)
    with engine.connect() as connection:
        opened = fx.vault.open_row(connection, "destination_account", row["id"])
    engine.dispose()
    assert mum_key.encode() in opened
    assert fingerprint and mum_key not in fingerprint  # a short mark, never the key
    assert mum_key not in _everything_written(fx)
    assert dad_key not in _everything_written(fx)
    assert fx.service.open_token(mum, fx.destination_id).reveal() == mum_key
    assert fx.service.open_token(dad, fx.destination_id).reveal() == dad_key


@pytest.mark.req("EF-USR-012")
def test_rotation_replaces_the_sealed_token_and_the_fingerprint(fx: Fixture) -> None:
    mum, first = _member(fx, "mum")
    before = _link(fx, "mum", first)
    old = fx.rows("SELECT secret_ct FROM destination_account")[0][0]
    second = fx.fake.add_member_key("mum.kv")
    again = _link(fx, "mum", second)
    assert again != before
    assert fx.rows("SELECT secret_ct FROM destination_account")[0][0] != old
    assert len(fx.accounts()) == 1  # the same row: one account per user and destination
    assert fx.service.open_token(mum, fx.destination_id).reveal() == second
    assert first not in _everything_written(fx) and second not in _everything_written(fx)
    assert [a for a, _ in fx.audit()] == [
        "destination_account.linked",
        "destination_account.rotated",
    ]


@pytest.mark.req("EF-USR-012")
def test_revocation_erases_the_token_and_is_audited(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    _link(fx, "mum", key)
    done = fx.send("DELETE", accounts_path(mum, fx.destination_id))
    assert done.status_code == 204
    assert fx.rows("SELECT secret_ct, token_fingerprint, revoked_at FROM destination_account")[0][
        :2
    ] == (None, None)
    assert fx.rows("SELECT revoked_at FROM destination_account")[0][0] is not None
    assert fx.service.open_token(mum, fx.destination_id) is None
    assert [a for a, _ in fx.audit()] == [
        "destination_account.linked",
        "destination_account.revoked",
    ]
    assert key not in _everything_written(fx)
    # the member links again: the row lives again, with a new token
    again = fx.fake.add_member_key("mum.kv")
    assert fx.put(mum, fx.destination_id, again, "mum.kv").status_code == 200
    assert fx.service.open_token(mum, fx.destination_id).reveal() == again


@pytest.mark.req("EF-USR-012")
def test_admin_revokes_any_account_but_never_reads_a_token(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    _link(fx, "mum", key)
    fx.as_user(ADMIN)
    listed = fx.send("GET", accounts_path(mum))
    assert listed.status_code == 200
    [item] = listed.json()["items"]
    assert set(item) == {
        "id", "user_id", "shelf_destination_id", "remote_user", "token_fingerprint",
        "verified_at", "revoked_at",
    }  # fmt: skip
    assert key not in listed.text
    assert fx.send("DELETE", accounts_path(mum, fx.destination_id)).status_code == 204
    assert fx.service.open_token(mum, fx.destination_id) is None
    [(_, target), *_] = fx.audit("destination_account.revoked")
    assert key not in target
    # the admin does not link an adult's account for them
    fresh = fx.fake.add_member_key("mum.kv")
    refused = fx.put(mum, fx.destination_id, fresh, "mum.kv")
    assert refused.status_code == 404
    assert fx.service.open_token(mum, fx.destination_id) is None


# --- never in a log, an audit, a bundle, an export, an error, a repr ------------------------------


@pytest.mark.req("EF-USR-012", "ENF-SEC-005")
def test_member_token_never_logged_nor_exported(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    mum, key = _member(fx, "mum")
    seen: list[str] = [key]
    with structlog.testing.capture_logs() as logs:
        fx.as_user("mum")
        bodies = [fx.put(mum, fx.destination_id, key, "mum.kv")]
        rotated = fx.fake.add_member_key("mum.kv")
        seen.append(rotated)
        bodies.append(fx.put(mum, fx.destination_id, rotated, "mum.kv"))
        wrong = fx.fake.add_member_key("somebody.else")
        seen.append(wrong)
        bodies.append(fx.put(mum, fx.destination_id, wrong, "mum.kv"))  # refused: not her account
        bodies.append(fx.put(mum, fx.destination_id, "not-a-key", "mum.kv"))
        bodies.append(fx.send("GET", accounts_path(mum)))
        bodies.append(fx.send("DELETE", accounts_path(mum, fx.destination_id)))
    out, err = capfd.readouterr()
    for token in seen:
        assert token not in repr(logs) + out + err
        assert token not in "".join(b.text + repr(dict(b.headers)) for b in bodies)
        assert token not in _everything_written(fx)
        assert token not in repr(fx.service) and token not in str(fx.service)
    # the whole key never enters the URL of a request to Kavita either (it is a header)
    for request in fx.fake.requests:
        assert not any(token in request.raw_url for token in seen)
    assert fx.fake.violations == []
    # the opened token is a value that does not print
    fx.put(mum, fx.destination_id, seen[1], "mum.kv")
    opened = fx.service.open_token(mum, fx.destination_id)
    assert seen[1] not in repr(opened) and seen[1] not in str(opened)
    assert seen[1] not in repr([opened]) and seen[1] not in f"{opened!r}{opened}"
    # the diagnostic bundle: a positive list, the table is not in it, in any form
    bundle = tmp_path / "bundle.zip"
    assert run_cli(monkeypatch, fx.settings, "diagnostic", "bundle", "--out", str(bundle)) == 0
    data = bundle.read_bytes()
    views = list(_views(data))
    for token in seen:
        assert not any(n in view for view in views for n in _needles(token))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert not any("destination_account" in n for n in archive.namelist())
    # the full export: the table is a credential table, left out by name and by shape
    from shelfhold.platform import export_all as full_export

    assert "destination_account" in full_export.EXCLUDED_TABLES
    assert "destination_account" not in {t.name for t in full_export._tables()}


@pytest.mark.req("EF-USR-012", "ENF-SEC-005")
def test_the_token_is_not_in_the_user_data_export(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    _link(fx, "mum", key)
    fx.as_user(ADMIN)
    exported = fx.send("GET", f"{PROFILES_PATH}/{mum}/export")
    assert exported.status_code == 200
    assert key not in exported.text
    assert "destination_account" not in exported.text


# --- the key must open the declared account ------------------------------


@pytest.mark.req("EF-USR-012")
def test_token_must_open_the_declared_account(fx: Fixture) -> None:
    mum, mum_key = _member(fx, "mum")
    other_key = fx.fake.add_member_key("somebody.else")
    fx.as_user("mum")
    refused = fx.put(mum, fx.destination_id, other_key, "mum.kv")  # opens another account
    assert refused.status_code == 422 and refused.json()["reason"] == "token_refused"
    assert "somebody" not in refused.text  # it does not say whose account the key opens
    unknown = fx.put(mum, fx.destination_id, "a-key-kavita-does-not-know", "mum.kv")
    assert unknown.status_code == 422 and unknown.json()["reason"] == "token_refused"
    assert fx.accounts() == []  # nothing is kept for a key that did not verify
    # Kavita compares names without case: "MUM.KV" is the account "mum.kv"
    assert fx.put(mum, fx.destination_id, mum_key, "MUM.KV").status_code == 200
    [(user, _dest, remote, fingerprint, revoked)] = fx.accounts()
    assert (user, remote, revoked) == (mum, "MUM.KV", 0) and fingerprint
    assert [r.path for r in fx.fake.requests_with_key(mum_key)] == ["/api/Account"]
    verified = fx.rows("SELECT verified_at FROM destination_account")[0][0]
    assert verified is not None


@pytest.mark.req("EF-USR-012")
def test_a_kavita_account_is_linked_to_one_member_per_destination(fx: Fixture) -> None:
    _member(fx, "mum")
    dad, dad_key = _member(fx, "dad")
    mum_key = fx.fake.add_member_key("mum.kv")
    _link(fx, "mum", mum_key)
    fx.as_user("dad")
    clash = fx.put(dad, fx.destination_id, mum_key, "mum.kv")  # dad claims mum's account
    assert clash.status_code == 409 and clash.json()["reason"] == "remote_user_taken"
    assert "mum" not in clash.text  # it does not say whose account it is
    assert [r[0] for r in fx.accounts()] == [fx.users["mum"]]
    assert dad_key  # unused: dad's own account is still free


@pytest.mark.req("EF-USR-012")
def test_the_verification_goes_through_the_guarded_fetcher(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    # the only reachable host is the declared Kavita: a binding whose address is not declared is
    # refused by the SSRF guard before the key leaves, and nothing is kept
    with closing(sqlite3.connect(fx.settings.database_path, isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        target, dest = new_ulid(), new_ulid()
        shelf = fx.rows("SELECT id FROM shelf")[0][0]
        add_row(db, "export_target", id=target, kind="kavita", name="kavita-metadata",
                config='{"url": "http://169.254.169.254:80"}')  # fmt: skip
        add_row(db, "shelf_destination", id=dest, shelf_id=shelf, target_id=target,
                transport="api", egress="foyer", root_subpath="meta", template="{series}",
                template_origin="default@1")  # fmt: skip
    fx.as_user("mum")
    before = len(fx.fake.requests)
    refused = fx.put(mum, dest, key, "mum.kv")
    assert refused.status_code == 422 and refused.json()["reason"] == "destination_unreachable"
    assert len(fx.fake.requests) == before  # the key was sent nowhere
    assert fx.accounts() == []
    assert key not in refused.text


@pytest.mark.req("EF-USR-012")
def test_a_destination_that_is_not_a_kavita_takes_no_account(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    fx.as_user("mum")
    refused = fx.put(mum, fx.foreign_destination_id, key, "mum.kv")
    assert refused.status_code == 422 and refused.json()["reason"] == "destination_not_supported"
    assert fx.put(mum, ULID_UNKNOWN, key, "mum.kv").status_code == 404
    assert fx.accounts() == [] and fx.fake.requests == []


@pytest.mark.req("EF-USR-012")
def test_a_sleeping_kavita_keeps_nothing(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    fx.fake.asleep = True
    fx.as_user("mum")
    done = fx.put(mum, fx.destination_id, key, "mum.kv")
    assert done.status_code == 422 and done.json()["reason"] == "destination_unreachable"
    assert fx.accounts() == [] and key not in done.text


@pytest.mark.req("EF-USR-012")
def test_the_body_takes_the_key_and_a_name_and_nothing_else(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    fx.as_user("mum")
    path = accounts_path(mum, fx.destination_id)
    for extra in ("api_key", "password", "secret", "user_id", "role"):
        bad = fx.send("PUT", path, {"token": key, "remote_user": "mum.kv", extra: "x"})
        assert bad.status_code == 422, extra
    for token in ("", " ", "a" * 513, 12, ["x"], None):
        assert fx.send("PUT", path, {"token": token, "remote_user": "mum.kv"}).status_code == 422
    for name in ("", "a" * 129, "mum\nkv", None):
        assert fx.send("PUT", path, {"token": key, "remote_user": name}).status_code == 422
    assert fx.accounts() == []
    assert key not in _everything_written(fx)


# --- a child's key: entered by a parent, as a reserved action ------------------------------


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_child_token_set_by_parent_as_reserved_action(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kid, kid_key = _member(fx, "kid", "child", max_age_rating=10)
    _member(fx, "mum")
    path = accounts_path(kid, fx.destination_id)
    # the child's Kavita account is the one the parent declared (EF-USR-017), not one she chooses
    fx.as_user(ADMIN)
    none_declared = fx.put(kid, fx.destination_id, kid_key)
    assert none_declared.status_code == 422
    assert none_declared.json()["reason"] == "child_account_not_declared"
    assert fx.send("PUT", f"{PROFILES_PATH}/{kid}/kavita-account",
                   {"kavita_username": "kid.kv"}).status_code == 200  # fmt: skip
    named = fx.put(kid, fx.destination_id, kid_key, "someone.else")  # a name of her own: refused
    assert named.status_code == 422
    # a child, a member and any key are refused, and nothing is written
    for username in ("kid", "mum"):
        fx.as_user(username)
        assert fx.put(kid, fx.destination_id, kid_key).status_code in (403, 404), username
        assert fx.send("GET", path.rsplit("/", 1)[0]).status_code in (403, 404), username
        assert fx.send("DELETE", path).status_code in (403, 404), username
    assert fx.accounts() == []
    assert run_cli(monkeypatch, fx.settings, "admin", "keys", "create", "--name", "k") == 0
    api_key = KEY.search(capsys.readouterr().out)
    assert api_key
    keyed = fx.api.client.request(
        "PUT", path, headers={"Authorization": f"Bearer {api_key.group(0)}"},
        json={"token": kid_key},
    )  # fmt: skip
    assert keyed.status_code == 403
    assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert fx.accounts() == []
    # a session older than 900 s is asked to prove again (secrets: fresh in every profile)
    fx.as_user(ADMIN)
    fx.api.clock.advance(timedelta(minutes=20))
    stale = fx.put(kid, fx.destination_id, kid_key)
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert fx.accounts() == []
    # a fresh admin session: it opens the declared account, it is kept for the child
    fx.as_user(ADMIN)
    done = fx.put(kid, fx.destination_id, kid_key)
    assert done.status_code == 200, done.text
    [(user, _d, remote, fingerprint, _r)] = fx.accounts()
    assert (user, remote) == (kid, "kid.kv") and fingerprint
    assert fx.service.open_token(kid, fx.destination_id).reveal() == kid_key
    assert RESERVED_ACTIONS["destination_account.link"].effects
    [(action, target)] = fx.audit("destination_account.linked")
    assert action and kid_key not in target and "kid.kv" not in target
    # the child cannot read what her parent entered, nor undo it
    fx.as_user("kid")
    assert fx.send("GET", path.rsplit("/", 1)[0]).status_code in (403, 404)
    assert fx.service.open_token(kid, fx.destination_id) is not None


@pytest.mark.req("EF-USR-012")
def test_a_child_account_follows_the_name_the_parent_declared(fx: Fixture) -> None:
    kid, kid_key = _member(fx, "kid", "child", max_age_rating=10)
    fx.as_user(ADMIN)
    assert fx.send("PUT", f"{PROFILES_PATH}/{kid}/kavita-account",
                   {"kavita_username": "kid.kv"}).status_code == 200  # fmt: skip
    assert fx.put(kid, fx.destination_id, kid_key).status_code == 200
    # the parent renames the declared account: the key that opened the old one no longer holds
    assert fx.send("PUT", f"{PROFILES_PATH}/{kid}/kavita-account",
                   {"kavita_username": "kid.other"}).status_code == 200  # fmt: skip
    assert fx.service.open_token(kid, fx.destination_id) is None
    assert [r[4] for r in fx.accounts()] == [1]  # revoked, its token erased
    assert [a for a, _ in fx.audit()][-1] == "destination_account.revoked"


# --- IDOR: one member never sets, reads or revokes another's ------------------------------


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_member_cannot_set_read_or_revoke_another_members_token(fx: Fixture) -> None:
    mum, mum_key = _member(fx, "mum")
    dad, dad_key = _member(fx, "dad")
    kid, kid_key = _member(fx, "kid", "child", max_age_rating=10)
    _link(fx, "dad", dad_key)
    before = fx.accounts()
    sealed_before = fx.rows("SELECT secret_ct FROM destination_account")
    requests_before = len(fx.fake.requests)
    fx.as_user("mum")
    for target in (dad, kid, ULID_UNKNOWN):  # the profile of another, and one that is not there
        path = accounts_path(target, fx.destination_id)
        put = fx.put(target, fx.destination_id, mum_key, "dad.kv")
        assert put.status_code == 404, target
        assert fx.send("GET", accounts_path(target)).status_code == 404, target
        assert fx.send("DELETE", path).status_code == 404, target
        # the same answer for a real profile and an invented one: nothing tells them apart
        assert put.json()["type"] == fx.put(ULID_UNKNOWN, fx.destination_id, mum_key).json()["type"]
    assert fx.accounts() == before
    assert fx.rows("SELECT secret_ct FROM destination_account") == sealed_before
    assert len(fx.fake.requests) == requests_before  # no key was tried against Kavita
    assert fx.service.open_token(dad, fx.destination_id).reveal() == dad_key
    # her own is hers
    assert fx.put(mum, fx.destination_id, mum_key, "mum.kv").status_code == 200
    assert [i["user_id"] for i in fx.send("GET", accounts_path(mum)).json()["items"]] == [mum]
    # an adult cannot link to another member's Kavita account by naming it
    stolen = fx.put(mum, fx.other_destination_id, dad_key, "dad.kv")
    assert stolen.status_code == 422 and stolen.json()["reason"] == "token_refused"
    assert kid_key  # the child's key is not mum's to try either


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_a_child_manages_no_account_of_her_own(fx: Fixture) -> None:
    kid, kid_key = _member(fx, "kid", "child", max_age_rating=10)
    fx.as_user("kid")
    for method, path, body in (
        ("PUT", accounts_path(kid, fx.destination_id), {"token": kid_key, "remote_user": "kid.kv"}),
        ("GET", accounts_path(kid), None),
        ("DELETE", accounts_path(kid, fx.destination_id), None),
    ):
        assert fx.send(method, path, body).status_code == 403, method
    assert fx.accounts() == [] and fx.fake.requests == []


# --- an API key links nothing ------------------------------


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_destination_account_routes_refused_to_api_keys(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mum, mum_key = _member(fx, "mum")
    _link(fx, "mum", mum_key)
    keys = []
    for scope in PRINCIPAL_KEYS:
        argv = ["admin", "keys", "create", "--name", f"k-{scope}", "--scope", scope]
        if scope == "request":
            argv += ["--subject", "mum"]
        assert run_cli(monkeypatch, fx.settings, *argv) == 0
        found = KEY.search(capsys.readouterr().out)
        assert found
        keys.append(found.group(0))
    before = fx.accounts()
    other = fx.fake.add_member_key("mum.kv")
    requests_before = len(fx.fake.requests)
    fx.api.client.cookies.clear()
    for api_key in keys:
        header = {"Authorization": f"Bearer {api_key}"}
        for target in (mum, ULID_UNKNOWN):  # the same refusal for a real id and an invented one
            for method, path, body in (
                ("PUT", accounts_path(target, fx.destination_id), {"token": other}),
                ("GET", accounts_path(target), None),
                ("DELETE", accounts_path(target, fx.destination_id), None),
            ):
                kwargs: dict[str, Any] = {"headers": header}
                if body is not None:
                    kwargs["json"] = body
                refused = fx.api.client.request(method, path, **kwargs)
                assert refused.status_code == 403, (method, target)
                assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert fx.accounts() == before
    assert len(fx.fake.requests) == requests_before
    assert fx.service.open_token(mum, fx.destination_id).reveal() == mum_key


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_the_routes_need_a_session_and_the_csrf_token(fx: Fixture) -> None:
    mum, key = _member(fx, "mum")
    fx.api.client.cookies.clear()
    anonymous = fx.api.client.put(accounts_path(mum, fx.destination_id), json={"token": key})
    assert anonymous.status_code in (401, 403)
    fx.as_user("mum")
    no_csrf = fx.api.client.put(
        accounts_path(mum, fx.destination_id), json={"token": key, "remote_user": "mum.kv"}
    )
    assert no_csrf.status_code in (401, 403)
    assert fx.accounts() == []


@pytest.mark.req("EF-USR-012", "EF-USR-024")
def test_link_and_revoke_are_declared_reserved_actions(fx: Fixture) -> None:
    link = RESERVED_ACTIONS["destination_account.link"]
    revoke = RESERVED_ACTIONS["destination_account.revoke"]
    assert link.needs_fresh_auth(restricted_profile_exists=False, hardened=False)  # a secret
    assert not revoke.needs_fresh_auth(restricted_profile_exists=False, hardened=False)
    mum, key = _member(fx, "mum")
    fx.as_user("mum")
    fx.api.clock.advance(timedelta(minutes=20))
    stale = fx.put(mum, fx.destination_id, key, "mum.kv")  # entering a key proves the password
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert fx.accounts() == []
