"""M6 L07 (EF-SRC-017, D14): the refusals of the signed index, exercised end to end.

Every key is made in the test (`pynacl`); no secret ever lives here (D-P3). What this file
covers is the branches a pin, a refresh and a wheel fetch refuse on: a malformed public key,
a malformed signature file, a malformed document, a rotation whose second signature says
something else, a repository unpinned or overtaken while its index was being read, a wheel
that is not the one the index vouches for — and the CLI commands of the index (`add`, `list`,
`refresh`, `remove`) with their own refusals: no database, no audit row, nothing pinned at
the address asked for.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fetcher.client import Download
from shelfhold.infrastructure.fetcher.errors import FetchError
from shelfhold.infrastructure.migrate import migrate
from shelfhold.plugin_host.index import (
    PluginIndexError,
    RepoStore,
    index_fetcher,
    parse_document,
    parse_public_key,
    verify_detached,
)
from tests.auth_env import ADMIN, AuthEnv, auth_env, db_rows, run_cli
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
HTTP_EXPIRES = "2026-12-01T00:00:00Z"  # 26 days after the auth fixture's clock
COMMENT = f"shelfhold-index v1 version=1 expires={EXPIRES}"


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


# -- the public key and the signature file ------------------------------------------------------


def test_a_malformed_public_key_is_a_typed_refusal() -> None:
    with pytest.raises(PluginIndexError) as caught:
        parse_public_key("%%% not base64 %%%")
    assert caught.value.code == "fingerprint_invalid"
    # base64, but not the minisign Ed blob: 10 bytes instead of 42
    with pytest.raises(PluginIndexError) as caught:
        parse_public_key(base64.b64encode(b"Ed" + secrets.token_bytes(8)).decode("ascii"))
    assert caught.value.code == "fingerprint_invalid"


def _signature(
    key: IndexKey, *, blob: bytes | None = None, lines: list[str] | None = None
) -> bytes:
    document = index_document(version=1, expires=EXPIRES)
    made = sign_index(document, key, comment=COMMENT).decode("ascii").splitlines()
    if blob is not None:
        made[1] = base64.b64encode(blob).decode("ascii")
    if lines is not None:
        made = lines
    return ("\n".join(made) + "\n").encode("ascii")


def _document() -> bytes:
    return index_document(version=1, expires=EXPIRES)


def _pinned(key: IndexKey) -> Any:
    return parse_public_key(key.public_b64)


def _not_four_lines(key: IndexKey) -> bytes:
    return _signature(key, lines=["untrusted comment: signature by nobody", "x"])


def _not_base64(key: IndexKey) -> bytes:
    lines = _signature(key).decode("ascii").splitlines()
    lines[1] = "%%% not base64 %%%"
    return ("\n".join(lines) + "\n").encode("ascii")


def _short_blob(key: IndexKey) -> bytes:
    return _signature(key, blob=b"ED" + secrets.token_bytes(8))


def _unknown_algorithm(key: IndexKey) -> bytes:
    return _signature(key, blob=b"XX" + key.raw_id + b"\x01" * 64)


def _no_comment_prefix(key: IndexKey) -> bytes:
    lines = _signature(key).decode("ascii").splitlines()
    lines[2] = COMMENT  # the text, without the line's prefix
    return ("\n".join(lines) + "\n").encode("ascii")


@pytest.mark.parametrize(
    "build",
    [_not_four_lines, _not_base64, _short_blob, _unknown_algorithm, _no_comment_prefix],
    ids=["not-four-lines", "not-base64", "short-blob", "unknown-algorithm", "no-comment-prefix"],
)
def test_a_malformed_signature_file_is_refused(build: Any) -> None:
    key = make_key()
    with pytest.raises(PluginIndexError) as caught:  # the key is well formed: the file is not
        verify_detached(_document(), build(key), _pinned(key))
    assert caught.value.code == "signature_invalid"


def test_a_signature_by_another_key_under_the_pinned_id_is_refused() -> None:
    pinned, stranger = make_key(), make_key()
    document = _document()
    digest = hashlib.blake2b(document, digest_size=64).digest()
    blob = b"ED" + pinned.raw_id + bytes(stranger.signing.sign(digest).signature)
    with pytest.raises(PluginIndexError) as caught:
        verify_detached(document, _signature(pinned, blob=blob), _pinned(pinned))
    assert caught.value.code == "bad_signature"


# -- the document ------------------------------------------------------------------------------


def _base() -> dict[str, Any]:
    return json.loads(index_document(version=1, expires=EXPIRES, plugins=[plugin_entry()]))


def _raw(**over: Any) -> bytes:
    raw = _base()
    raw.update(over)
    return json.dumps(raw).encode("utf-8")


@pytest.mark.parametrize(
    "build",
    [
        lambda: _raw(plugins=[plugin_entry(key="")]),  # a plugin key that is not text
        lambda: _raw(plugins=[plugin_entry(sha256="a" * 64)]),  # no sha256 prefix
        lambda: _raw(plugins=[plugin_entry(sha256="sha256:" + "g" * 64)]),  # not hexadecimal
        lambda: _raw(plugins=[plugin_entry(api=0)]),  # the api version is not positive
        lambda: _raw(plugins=[plugin_entry(wheel_url="ftp://example.org/a.whl")]),  # not web
        lambda: _raw(plugins=["acme"]),  # an entry that is not an object
        lambda: _raw(plugins=[plugin_entry(surprise=1)]),  # an unknown member
        lambda: _raw(plugins=[plugin_entry(replaced_by=7)]),  # the replacing key is not text
        lambda: b"[]",  # the index is not a JSON object
        lambda: _raw(format=2),  # the format is not 1
        lambda: _raw(version=0),  # the version is not positive
        lambda: _raw(version=2**63),  # the version is out of the database's range
        lambda: _raw(plugins={}),  # the plugins are not a list
        lambda: _raw(revoked=[]),  # the revocations are not an object
        lambda: _raw(revoked={"plugin_keys": [], "wheel_sha256": [], "extra": []}),
        lambda: _raw(revoked={"plugin_keys": "acme", "wheel_sha256": []}),  # not lists
        lambda: _raw(expires="next tuesday"),  # the expiry is not an ISO 8601 date
        lambda: _raw(expires="2027-03-01T00:00:00"),  # the expiry is not an instant
    ],
    ids=[
        "key-not-text",
        "sha256-no-prefix",
        "sha256-not-hex",
        "api-not-positive",
        "wheel-not-web",
        "entry-not-object",
        "entry-unknown-member",
        "replaced-by-not-text",
        "not-an-object",
        "format-not-1",
        "version-not-positive",
        "version-out-of-range",
        "plugins-not-list",
        "revoked-not-object",
        "revoked-unknown-member",
        "revoked-not-lists",
        "expiry-not-iso",
        "expiry-not-an-instant",
    ],
)
def test_a_malformed_document_is_refused(build: Any) -> None:
    with pytest.raises(PluginIndexError) as caught:
        parse_document(build())
    assert caught.value.code == "document_invalid"


def test_a_well_formed_document_is_believed() -> None:
    wheel = "sha256:" + "c" * 64
    document = parse_document(
        index_document(
            version=2,
            expires=EXPIRES,
            plugins=[plugin_entry(key="acme", sha256=wheel)],
            plugin_keys=["orphan"],
            wheel_sha256=[wheel],
        )
    )
    assert document.version == 2
    assert [entry.key for entry in document.plugins] == ["acme"]
    assert document.plugins[0].sha256 == wheel
    assert document.revoked_plugin_keys == {"orphan"}
    assert document.revoked_wheel_sha256 == {wheel}
    assert document.expires == datetime(2027, 3, 1, tzinfo=UTC)


# -- the pin and the refresh --------------------------------------------------------------------


def test_a_pin_refuses_an_unplain_address_and_a_double_pin(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    try:
        with pytest.raises(PluginIndexError) as caught:
            store.add("ftp://index.example.org/index.json", key.public_b64)
        assert caught.value.code == "url_invalid"
        record = store.add(URL, key.public_b64)
        assert (record.url, record.key_id) == (URL, key.key_id)
        with pytest.raises(PluginIndexError) as caught:  # the same address twice: refused
            store.add(URL, make_key().public_b64)
        assert caught.value.code == "already_pinned"
        assert [pinned.url for pinned in store.list()] == [URL]
    finally:
        engine.dispose()


def test_the_index_client_is_the_host_fetcher_behind_the_ssrf_guard() -> None:
    from shelfhold.composition import default_network

    fetcher = index_fetcher(default_network())
    try:
        with pytest.raises(FetchError) as caught:  # a loopback address is never fetched
            asyncio.run(fetcher.get("http://127.0.0.1:9/index.json"))
        assert caught.value.code == "ssrf.blocked"
    finally:
        asyncio.run(fetcher.aclose())


def test_a_rotation_whose_second_signature_says_something_else_is_refused(
    settings: Settings,
) -> None:
    engine, store = _store(settings)
    old, new = make_key(), make_key()
    store.add(URL, old.public_b64)
    try:
        document = index_document(version=1, expires=EXPIRES, next_pubkey=new.public_b64)
        fetcher = FakeFetcher().serving(
            URL, document, sign_index(document, old, comment=trusted_comment(1, EXPIRES))
        )
        # the next key signs the document, but its comment says another version
        fetcher.documents[URL + ".next.minisig"] = sign_index(
            document, new, comment=trusted_comment(2, EXPIRES)
        )
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, fetcher)
        assert caught.value.code == "rotation_refused"
        pinned = store.list()[0]
        assert (pinned.key_id, pinned.last_version) == (old.key_id, 0)  # the pin did not move
    finally:
        engine.dispose()


class VanishingFetcher(FakeFetcher):
    """The repository is unpinned while its index is being read: the refresh must refuse."""

    def __init__(self, store: RepoStore, url: str) -> None:
        super().__init__()
        self._store, self._url, self._removed = store, url, False

    async def get(self, url: str, **kwargs: Any) -> Any:
        if not self._removed:
            self._removed = True
            self._store.remove(self._url)
        return await super().get(url, **kwargs)


def test_a_refresh_of_a_repository_unpinned_meanwhile_is_refused(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        fetcher = VanishingFetcher(store, URL)
        _served(fetcher, key, version=1)
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, fetcher)
        assert caught.value.code == "not_found"
        assert store.list() == []  # the pin is gone, nothing of the index was kept
    finally:
        engine.dispose()


class OvertakenFetcher(FakeFetcher):
    """Another refresh lands while this one reads the network: the floor moves underneath."""

    def __init__(self, store: RepoStore, url: str, other: FakeFetcher) -> None:
        super().__init__()
        self._store, self._url, self._other, self._done = store, url, other, False

    async def get(self, url: str, **kwargs: Any) -> Any:
        if not self._done:
            self._done = True
            record = next(r for r in self._store.list() if r.url == self._url)
            await self._store.refresh(record, self._other)
        return await super().get(url, **kwargs)


def test_a_refresh_overtaken_by_the_same_version(settings: Settings) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    try:
        # the same number said the same thing: the slower refresh changes nothing
        other = FakeFetcher()
        _served(other, key, version=3)
        slow = OvertakenFetcher(store, URL, other)
        _served(slow, key, version=3)
        assert _refresh(store, slow).applied is False
        assert store.list()[0].last_version == 3

        # the same number said something else: refused, the kept index stands
        other = FakeFetcher()
        _served(other, key, version=5, plugins=[plugin_entry(key="other")])
        slow = OvertakenFetcher(store, URL, other)
        _served(slow, key, version=5)
        with pytest.raises(PluginIndexError) as caught:
            _refresh(store, slow)
        assert caught.value.code == "same_version"
        assert store.list()[0].last_version == 5
    finally:
        engine.dispose()


# -- the wheel ----------------------------------------------------------------------------------


class LyingFetcher(FakeFetcher):
    """A download that vouches for the entry's hash while it writes other bytes."""

    def __init__(self, sha256: str) -> None:
        super().__init__()
        self._sha256 = sha256

    async def download(self, url: str, sink: Any, **kwargs: Any) -> Download:
        sink.write(b"not the wheel the index vouches for")
        return Download(status=200, headers={}, size=34, sha256=self._sha256)


def test_a_wheel_that_is_not_vouched_for_is_refused_and_leaves_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    engine, store = _store(settings)
    key = make_key()
    store.add(URL, key.public_b64)
    wheels = tmp_path / "wheels"  # the settings fixture keeps its own folders in tmp_path
    wheel = b"a wheel the index vouches for"
    digest = "sha256:" + hashlib.sha256(wheel).hexdigest()
    try:
        fetcher = FakeFetcher()
        _served(fetcher, key, version=1, plugins=[plugin_entry(sha256=digest)])
        entry = _refresh(store, fetcher).document.plugins[0]

        # the Fetcher itself refuses the address: a typed refusal, nothing on disk
        blocked = FakeFetcher(blocked={WHEEL_URL})
        with pytest.raises(PluginIndexError) as caught:
            asyncio.run(store.fetch_wheel(entry, blocked, wheels))
        assert caught.value.code == "fetch_refused"
        assert list(wheels.iterdir()) == []

        # no wheel is a refusal, not an empty wheel
        missing = FakeFetcher()
        with pytest.raises(PluginIndexError) as caught:
            asyncio.run(store.fetch_wheel(entry, missing, wheels))
        assert caught.value.code == "unreachable"
        assert list(wheels.iterdir()) == []

        # the stream's word is not enough: the file is read back, and it is not the wheel
        lying = LyingFetcher(entry.sha256)
        with pytest.raises(PluginIndexError) as caught:
            asyncio.run(store.fetch_wheel(entry, lying, wheels))
        assert caught.value.code == "wheel_hash_mismatch"
        assert list(wheels.iterdir()) == []
    finally:
        engine.dispose()


# -- the CLI: add, list, refresh, remove --------------------------------------------------------


def test_the_index_commands_need_a_database(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not settings.database_path.exists()  # nothing was migrated: nothing can be pinned
    for argv in (
        ("plugin", "index", "add", URL, "--pubkey", make_key().public_b64),
        ("plugin", "index", "list"),
        ("plugin", "index", "refresh", URL),
        ("plugin", "index", "remove", URL),
    ):
        assert run_cli(monkeypatch, settings, *argv) == 1
    assert "migrate" in capsys.readouterr().err


def test_the_index_list_shows_every_pinned_repository(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(settings)
    one, two = make_key(), make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", one.public_b64)
        == 0
    )
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "index", "add", OTHER_URL, "--pubkey", two.public_b64
        )
        == 0
    )
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "plugin", "index", "list") == 0
    out = capsys.readouterr().out
    assert URL in out and OTHER_URL in out
    assert one.key_id in out and two.key_id in out  # the fingerprint is what the admin checks
    assert "v0" in out and "expire" in out  # nothing believed yet, no expiry kept


def test_the_index_refresh_of_an_unpinned_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(settings)
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", make_key().public_b64
        )
        == 0
    )
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", OTHER_URL) == 1
    assert "not_found" in capsys.readouterr().err


def test_the_index_refresh_applies_repeats_and_refuses(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(settings)
    key = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", key.public_b64)
        == 0
    )
    capsys.readouterr()
    expires = (datetime.now(UTC) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetcher = FakeFetcher()
    document = index_document(version=1, expires=expires)
    fetcher.serving(URL, document, sign_index(document, key, comment=trusted_comment(1, expires)))
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _n: fetcher)

    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    out = capsys.readouterr().out
    assert URL in out and "appliqué" in out and "version 1" in out  # the index was applied
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            kept = connection.execute(
                sa.text("SELECT last_version FROM plugin_repo WHERE url = :u"), {"u": URL}
            ).scalar()
        assert kept == 1
    finally:
        engine.dispose()

    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 0
    assert "inchangé" in capsys.readouterr().out  # the same index again: nothing to do

    past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    expired = index_document(version=2, expires=past)
    fetcher.serving(URL, expired, sign_index(expired, key, comment=trusted_comment(2, past)))
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 1
    assert "Refusé (expired)" in capsys.readouterr().err
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            kept = connection.execute(
                sa.text("SELECT last_version FROM plugin_repo WHERE url = :u"), {"u": URL}
            ).scalar()
        assert kept == 1  # nothing of an expired index is kept
    finally:
        engine.dispose()


def test_without_its_audit_row_nothing_changes(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    migrate(settings)
    key = make_key()
    assert (
        run_cli(monkeypatch, settings, "plugin", "index", "add", URL, "--pubkey", key.public_b64)
        == 0
    )

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the audit trail is gone")

    monkeypatch.setattr("shelfhold.plugin_host.install.audit", broken)
    # an add whose audit row cannot be written leaves no pin behind
    assert (
        run_cli(
            monkeypatch, settings, "plugin", "index", "add", OTHER_URL, "--pubkey", key.public_b64
        )
        == 1
    )
    # a removal without its audit row changes nothing
    assert run_cli(monkeypatch, settings, "plugin", "index", "remove", URL) == 1
    # a refresh without its audit row believes nothing
    assert run_cli(monkeypatch, settings, "plugin", "index", "refresh", URL) == 1
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT url FROM plugin_repo")).fetchall()
        assert rows == [(URL,)]  # the second pin was rolled back, the first one stands
    finally:
        engine.dispose()


# -- the HTTP surface ---------------------------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        assert env.login(ADMIN).status_code == 200
        yield env


def test_the_refresh_of_an_unknown_repository_answers_a_typed_problem(api: AuthEnv) -> None:
    answer = api.post(f"/api/v1/plugin-repos/{URL}/refresh")
    assert answer.status_code == 404
    assert "plugin.index-not-found" in answer.json()["type"]


def test_the_refresh_over_http_applies_the_index_and_leaves_its_audit_row(
    api: AuthEnv, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = "/api/v1/plugin-repos"
    key = make_key()
    assert api.post(base, json={"url": URL, "pubkey": key.public_b64}).status_code == 201
    document = index_document(version=1, expires=HTTP_EXPIRES)
    fetcher = FakeFetcher().serving(
        URL, document, sign_index(document, key, comment=trusted_comment(1, HTTP_EXPIRES))
    )
    monkeypatch.setattr("shelfhold.main.index_fetcher", lambda _network: fetcher)
    answer = api.post(f"{base}/{URL}/refresh")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"url": URL, "version": 1, "applied": True, "rotated": False}
    rows = db_rows(
        settings, "SELECT action, target FROM audit WHERE action = 'plugin.index_refresh'"
    )
    assert rows == [("plugin.index_refresh", URL)]
    listed = api.client.get(base)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["last_version"] == 1
