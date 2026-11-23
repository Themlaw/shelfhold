"""M6 L07 (EF-SRC-017, D14, ADR-0056): the signed plugin index and its client.

Every key is made in the test (`pynacl`); no secret ever lives in the repo, the fixtures or the
CI (D-P3). The index is a minisign-signed document: prehashed `ED` signature, a trusted comment
bound to the version and the expiry, a monotone `version`, `expires` bounded to 90 days, sha256
per wheel, revocations that stay seen, rotation by double signature — and **zero repository
preconfigured**: adding one is the admin's, by URL *and* public key, a reserved action.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host.index import PluginIndexError, RepoStore
from tests.auth_env import (
    ADMIN,
    CSRF_COOKIE,
    CSRF_HEADER,
    PASSWORD,
    AuthEnv,
    auth_env,
    db_rows,
    run_cli,
)
from tests.plugin_index_env import (
    WHEEL_URL,
    FakeFetcher,
    IndexKey,
    index_document,
    make_key,
    plugin_entry,
    sign_index,
    trusted_comment,
)

pytestmark = pytest.mark.req("EF-SRC-017")

URL = "https://index.example.org/index.json"
OTHER_URL = "https://other.example.org/index.json"
T0 = datetime(2027, 1, 5, 9, 0, tzinfo=UTC)
EXPIRES = "2027-03-01T00:00:00Z"  # 55 days out: inside the 90-day bound
SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "plugin_index"


def _store(settings: Settings) -> tuple[sa.Engine, RepoStore]:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    return engine, RepoStore(engine, now=lambda: T0)


def _served(
    fetcher: FakeFetcher, key: IndexKey, *, version: int, expires: str = EXPIRES, **over: Any
) -> bytes:
    document = index_document(version=version, expires=expires, **over)
    fetcher.serving(
        URL, document, sign_index(document, key, comment=trusted_comment(version, expires))
    )
    return document


def _refresh(store: RepoStore, fetcher: FakeFetcher, url: str = URL) -> Any:
    record = next(r for r in store.list() if r.url == url)
    return asyncio.run(store.refresh(record, fetcher))


# -- the signature, the trusted comment, the monotone version ----------------------------------


def test_signed_index_monotonic_version_rejects_rollback(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        fetcher = FakeFetcher()
        _served(fetcher, key, version=4)
        assert _refresh(store, fetcher).applied
        assert store.list()[0].last_version == 4

        older = FakeFetcher()
        _served(older, key, version=2)  # a replayed index, however well signed
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, older)
        assert caught.value.code == "rollback"
        assert store.list()[0].last_version == 4

        again = FakeFetcher()
        _served(again, key, version=4)  # the same number, the same content: no effect
        assert _refresh(store, again).applied is False
        assert store.list()[0].last_version == 4

        equivocal = FakeFetcher()
        _served(equivocal, key, version=4, plugins=[plugin_entry(key="other")])
        with pytest.raises(PluginIndexError) as caught:  # two indexes for one number: refused
            _refresh(store, equivocal)
        assert caught.value.code == "same_version"
        assert store.list()[0].last_version == 4

        newer = FakeFetcher()
        _served(newer, key, version=5)
        assert _refresh(store, newer).applied
        assert store.list()[0].last_version == 5
    finally:
        engine.dispose()


def test_index_expired_rejected(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        past = FakeFetcher()
        _served(past, key, version=1, expires="2026-12-01T00:00:00Z")
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, past)
        assert caught.value.code == "expired"
        assert store.list()[0].last_version == 0  # nothing of an expired index is kept

        far = FakeFetcher()
        _served(
            far, key, version=1, expires=(T0 + timedelta(days=366)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        with pytest.raises(PluginIndexError) as caught:  # more than 90 days out: refused too
            _refresh(store, far)
        assert caught.value.code == "expires_too_far"

        soon = FakeFetcher()
        _served(
            soon, key, version=1, expires=(T0 + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        assert _refresh(store, soon).applied
        assert store.list()[0].expires is not None
    finally:
        engine.dispose()


def test_index_trusted_comment_bound_to_version(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        document = index_document(version=3, expires=EXPIRES)

        def refused(signature: str, code: str) -> None:
            fetcher = FakeFetcher().serving(URL, document, signature)
            with pytest.raises(PluginIndexError) as caught:
                _refresh(store, fetcher)
            assert caught.value.code == code
            assert store.list()[0].last_version == 0

        # the trusted comment is signed and says the document's own version and expiry
        refused(
            sign_index(document, key, comment=trusted_comment(2, EXPIRES)),
            "trusted_comment_mismatch",
        )
        refused(
            sign_index(document, key, comment=trusted_comment(3, "2027-04-01T00:00:00Z")),
            "trusted_comment_mismatch",
        )
        refused(
            sign_index(document, key, comment=trusted_comment(3, EXPIRES), global_signature=False),
            "bad_global_signature",
        )
        # the old non-prehashed format is refused, whoever signed it
        refused(
            sign_index(document, key, comment=trusted_comment(3, EXPIRES), prehashed=False),
            "legacy_signature",
        )
        # the signature must carry the id of the pinned key
        refused(
            sign_index(document, key, comment=trusted_comment(3, EXPIRES), key_id=b"\x00" * 8),
            "key_mismatch",
        )
        # the public sample document, signed as it should be, is accepted (a public vector)
        sample = (SAMPLE / "index.json").read_bytes()
        fetcher = FakeFetcher().serving(
            URL, sample, sign_index(sample, key, comment=trusted_comment(1, "2027-02-01T00:00:00Z"))
        )
        assert _refresh(store, fetcher).applied
        assert store.list()[0].last_version == 1
    finally:
        engine.dispose()


# -- revocation and rotation -------------------------------------------------------------------


def test_revocation_and_double_signature_rotation(settings: Settings) -> None:
    engine, store = _store(settings)
    old_key, new_key = make_key(), make_key()
    wheel = "sha256:" + "c" * 64
    store.add(URL, old_key.public_b64)
    try:
        first = FakeFetcher()
        _served(first, old_key, version=2, plugin_keys=["acme"], wheel_sha256=[wheel])
        assert _refresh(store, first).applied
        assert store.revoked_plugin_keys() == {"acme"}
        assert store.revoked_wheel_sha256() == {wheel}

        second = FakeFetcher()  # a later index that omits them changes nothing
        _served(second, old_key, version=3)
        assert _refresh(store, second).applied
        assert store.revoked_plugin_keys() == {"acme"}
        assert store.revoked_wheel_sha256() == {wheel}

        # rotation: the same document signed by the old key and by the next one
        rotating = FakeFetcher()
        document = index_document(version=4, expires=EXPIRES, next_pubkey=new_key.public_b64)
        rotating.serving(
            URL, document, sign_index(document, old_key, comment=trusted_comment(4, EXPIRES))
        )
        rotating.documents[URL + ".next.minisig"] = sign_index(
            document, new_key, comment=trusted_comment(4, EXPIRES)
        )
        outcome = _refresh(store, rotating)
        assert outcome.applied and outcome.rotated
        pinned = store.list()[0]
        assert (pinned.pubkey, pinned.key_id) == (new_key.public_b64, new_key.key_id)

        after = FakeFetcher()  # the next index is signed by the new key alone
        _served(after, new_key, version=5)
        assert _refresh(store, after).applied
        assert store.list()[0].last_version == 5

        # a new key alone never takes the pin: while the old one is pinned, an index that
        # names a next key but is signed by that next key is refused
        store.add(OTHER_URL, old_key.public_b64)
        alone = FakeFetcher()
        document = index_document(version=1, expires=EXPIRES, next_pubkey=new_key.public_b64)
        alone.serving(
            OTHER_URL, document, sign_index(document, new_key, comment=trusted_comment(1, EXPIRES))
        )
        alone.documents[OTHER_URL + ".next.minisig"] = sign_index(
            document, new_key, comment=trusted_comment(1, EXPIRES)
        )
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, alone, url=OTHER_URL)
        assert caught.value.code in ("key_mismatch", "rotation_refused")

        # the second signature is part of the rotation: without it, the pin does not move
        half = FakeFetcher()
        document = index_document(version=2, expires=EXPIRES, next_pubkey=new_key.public_b64)
        half.serving(
            OTHER_URL, document, sign_index(document, old_key, comment=trusted_comment(2, EXPIRES))
        )
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, half, url=OTHER_URL)
        assert caught.value.code == "rotation_refused"
        assert store.list()[0].last_version == 5  # the first repository is untouched
    finally:
        engine.dispose()


# -- the wheel, the Fetcher, the fresh install -------------------------------------------------


def test_wheel_sha256_verified(settings: Settings, tmp_path: Path) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    wheel = b"a wheel the index vouches for"
    digest = "sha256:" + hashlib.sha256(wheel).hexdigest()
    try:
        fetcher = FakeFetcher()
        _served(fetcher, key, version=1, plugins=[plugin_entry(sha256=digest)])
        entry = _refresh(store, fetcher).document.plugins[0]

        fetcher.wheels[WHEEL_URL] = wheel
        fetched = asyncio.run(store.fetch_wheel(entry, fetcher, tmp_path))
        assert fetched.read_bytes() == wheel
        assert WHEEL_URL in fetcher.calls  # the Fetcher carried it

        # a wheel that is not what the index vouches for is refused, and nothing is left
        fetcher.wheels[WHEEL_URL] = b"another wheel entirely"
        with pytest.raises(PluginIndexError) as caught:
            asyncio.run(store.fetch_wheel(entry, fetcher, tmp_path))
        assert caught.value.code == "wheel_hash_mismatch"
        # the settings fixture keeps its `data` and `store` folders here: what must be left
        # alone is no staging file — the only file is the wheel accepted before
        assert [p for p in tmp_path.iterdir() if p.is_file()] == [fetched]
    finally:
        engine.dispose()


def test_index_refresh_goes_through_fetcher(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        fetcher = FakeFetcher()
        _served(fetcher, key, version=1)
        assert _refresh(store, fetcher).applied
        assert fetcher.calls == [URL, URL + ".minisig"]  # the Fetcher is the only way out

        blocked = FakeFetcher(blocked={URL})  # the SSRF guard refuses: nothing is kept
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, blocked)
        assert caught.value.code == "fetch_refused"
        assert store.list()[0].last_version == 1

        missing = FakeFetcher()  # no answer is a refusal, not an empty index
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, missing)
        assert caught.value.code == "unreachable"
    finally:
        engine.dispose()


def test_fresh_install_zero_repo_preconfigured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store = _store(settings)
    try:
        assert store.list() == []  # no repository ships with the product
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM plugin_repo")).scalar() == 0
    finally:
        engine.dispose()
    assert settings.plugin_index_url is None  # and no setting preconfigures one
    assert run_cli(monkeypatch, settings, "plugin", "index", "list") == 0


# -- adding and removing a repository: the admin's, by URL and key fingerprint -----------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login(ADMIN).status_code == 200
        yield env


def _key_of(api: AuthEnv, username: str, **kwargs: Any) -> str:
    from shelfhold.identity.public import local_auth

    engine = create_db_engine(api.settings.database_path)
    try:
        return (
            local_auth(engine, api.settings)
            .create_api_key(name="k", username=username, **kwargs)
            .value
        )
    finally:
        engine.dispose()


def _send(api: AuthEnv, client: Any, method: str, path: str, body: Any = None) -> Any:
    headers = (
        {}
        if "Authorization" in client.headers
        else {CSRF_HEADER: client.cookies.get(CSRF_COOKIE) or "made-up"}
    )
    return client.request(method, path, json=body, headers=headers)


@pytest.mark.req("EF-SRC-017", "ENF-SEC-010", "EF-USR-024")
def test_index_add_is_reserved_and_needs_key_fingerprint(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from tests.integration.test_child_surfaces import _routes

    assert "plugin.index_add" in RESERVED_ACTIONS
    assert "plugin.index_remove" in RESERVED_ACTIONS
    base = "/api/v1/plugin-repos"
    routes = {
        tuple(entry.split(" ", 1))
        for entry in _routes(api.app)
        if entry.split(" ", 1)[1] == base or entry.split(" ", 1)[1].startswith(base + "/")
    }
    assert ("GET", base) in routes and ("POST", base) in routes
    assert any(m == "DELETE" for m, _ in routes), routes  # the removal, by repository
    assert any(m == "POST" and p.endswith("/refresh") for m, p in routes), routes

    key = make_key()
    made = api.post(base, json={"url": URL, "pubkey": key.public_b64})
    assert made.status_code == 201, made.text
    assert made.json()["key_id"] == key.key_id  # the fingerprint is shown, the key is pinned

    # the fingerprint is not optional, whatever the session
    refused = api.post(base, json={"url": OTHER_URL})
    assert refused.status_code == 422
    assert "fingerprint" in refused.json()["type"]

    # reserved: never a key, however wide its scope
    admin_key = api.other_client("10.0.0.24")
    admin_key.headers["Authorization"] = f"Bearer {_key_of(api, 'admin')}"
    sent = _send(api, admin_key, "POST", base, {"url": OTHER_URL, "pubkey": key.public_b64})
    assert sent.status_code == 403
    assert _send(api, admin_key, "DELETE", f"{base}/{URL}").status_code == 403

    # a child and a member are refused on every route of the surface
    for name, role, ip in (("kid", "child", "10.0.0.2"), ("ana", "member", "10.0.0.3")):
        auth = api.app.state.auth_gate.auth
        auth.profile_add(
            name, PASSWORD, role, **({"max_age_rating": 10} if role == "child" else {})
        )
        client = api.other_client(ip)
        assert api.login(name, client=client).status_code == 200
        for method, path, body in (
            ("GET", base, None),
            ("POST", base, {"url": OTHER_URL, "pubkey": key.public_b64}),
            ("DELETE", f"{base}/{URL}", None),
            ("POST", f"{base}/{URL}/refresh", None),
        ):
            answer = _send(api, client, method, path, body)
            assert answer.status_code == 403, (name, method, answer.status_code)

    # a stale session reauthenticates first
    api.clock.advance(timedelta(seconds=901))
    sent = _send(api, api.client, "POST", base, {"url": OTHER_URL, "pubkey": key.public_b64})
    assert sent.status_code == 401

    # the host CLI pins a repository too, and never without the key
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "index", "add", OTHER_URL, "--pubkey", key.public_b64
        )
        == 0
    )
    assert run_cli(monkeypatch, settings, "plugin", "index", "add", URL) != 0
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT url FROM plugin_repo")).fetchall()
        assert rows == [(URL,), (OTHER_URL,)]
    finally:
        engine.dispose()

    # removing is the admin's too, and a repository added back starts from zero
    assert run_cli(monkeypatch, settings, "plugin", "index", "remove", URL) == 0
    assert run_cli(monkeypatch, settings, "plugin", "index", "remove", URL) != 0  # already gone
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", key.public_b64)
        == 0
    )
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT last_version FROM plugin_repo WHERE url = :u"), {"u": URL}
                ).scalar()
                == 0
            )
    finally:
        engine.dispose()


@pytest.mark.req("EF-SRC-017", "ENF-OBS-003", "EF-USR-024")
def test_index_add_and_remove_audited_over_http(api: AuthEnv, settings: Settings) -> None:
    """ENF-OBS-003: over HTTP too the pin and its removal leave their audit row — the action,
    the repository and its key fingerprint, the account that acted."""
    base = "/api/v1/plugin-repos"
    key = make_key()
    made = api.post(base, json={"url": URL, "pubkey": key.public_b64})
    assert made.status_code == 201, made.text
    actor = db_rows(settings, 'SELECT id FROM "user" WHERE username = ?', ADMIN)[0][0]
    rows = db_rows(
        settings, "SELECT action, target, actor_id FROM audit WHERE action = 'plugin.index_add'"
    )
    assert rows == [("plugin.index_add", f"{URL}:{key.key_id}", actor)], rows
    assert _send(api, api.client, "DELETE", f"{base}/{URL}").status_code == 200
    rows = db_rows(
        settings, "SELECT action, target, actor_id FROM audit WHERE action = 'plugin.index_remove'"
    )
    assert rows == [("plugin.index_remove", URL, actor)], rows


@pytest.mark.req("EF-SRC-017", "ENF-OBS-003", "EF-USR-024")
def test_index_refusals_audited_over_http(
    api: AuthEnv, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal over HTTP leaves its trace like the CLI's: the address and the closed code."""
    base = "/api/v1/plugin-repos"
    actor = db_rows(settings, 'SELECT id FROM "user" WHERE username = ?', ADMIN)[0][0]
    # no signer key: the typed refusal, and its audit row
    refused = api.post(base, json={"url": URL})
    assert refused.status_code == 422
    assert "fingerprint" in refused.json()["type"]
    rows = db_rows(
        settings,
        "SELECT action, target, actor_id FROM audit WHERE action = 'plugin.index_add_refused'",
    )
    assert rows == [("plugin.index_add_refused", f"{URL}:fingerprint_required", actor)], rows
    # a refresh that fails is audited too: the address and the closed code, never a body
    key = make_key()
    assert api.post(base, json={"url": URL, "pubkey": key.public_b64}).status_code == 201
    monkeypatch.setattr("shelfhold.main.index_fetcher", lambda _network: FakeFetcher())
    answer = api.post(f"{base}/{URL}/refresh")
    assert answer.status_code == 422, answer.text
    rows = db_rows(
        settings,
        "SELECT action, target, actor_id FROM audit WHERE action = 'plugin.index_refresh_refused'",
    )
    assert rows == [("plugin.index_refresh_refused", f"{URL}:unreachable", actor)], rows
