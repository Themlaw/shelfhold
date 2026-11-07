"""ENF-SEC-005, EF-INT-023, EF-BIB-053 (M1b L06): `shelfhold secrets import`, played end to end.

The command is the real one (`cli.main`, the channel of the host or the socket of the server), the
routes are those of `create_app()`, the keyring and the database are files. The state it leaves,
`tombstone_key_unknown`, is made by a real purge and a keyring that lost its key.
"""
# ruff: noqa: S105, S106, E501 (synthetic passphrases; the scenarios read better long)

import base64
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings, load_settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import coherence, metadata
from shelfhold.infrastructure.secrets import diagnostics, registry
from shelfhold.infrastructure.secrets.envelope import KEY_BYTES
from shelfhold.infrastructure.secrets.export import armor, encrypt_export
from shelfhold.infrastructure.secrets.keyring import Key, Keyring, KeyringStore
from shelfhold.infrastructure.secrets.vault import SecretVault
from tests.auth_env import auth_env, db_rows, run_cli
from tests.l50_env import seeded
from tests.l53_env import status_json, waiting_states

secret_key = metadata.tables["secret_key"]
source_config = metadata.tables["source_config"]

pytestmark = pytest.mark.req("ENF-SEC-005", "EF-INT-023", "EF-BIB-053")

PASSPHRASE = "synthetic import passphrase 7Q"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")


def private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def passphrase_file(tmp_path: Path, text: str = PASSPHRASE, mode: int = 0o600) -> Path:
    return private(tmp_path / "passphrase", text + "\n", mode)


def store_of(settings: Settings) -> KeyringStore:
    return diagnostics.store_for(settings)


def export_file(tmp_path: Path, keyring: Keyring, passphrase: str = PASSPHRASE) -> Path:
    return private(tmp_path / "export.age", encrypt_export(keyring, passphrase))


def import_cli(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    source: Path,
    *,
    passphrase: Path | None = None,
) -> int:
    return run_cli(
        monkeypatch,
        settings,
        "secrets",
        "import",
        "--passphrase-file",
        str(passphrase or passphrase_file(tmp_path)),
        "--in",
        str(source),
    )


def audits(settings: Settings) -> list[tuple[object, ...]]:
    """The rows of the import itself (the identity layer adds its own at the door of a route)."""
    return db_rows(
        settings,
        "SELECT actor_id, target, session_id FROM audit "
        "WHERE action = 'secrets.import' AND target LIKE 'kids:%'",
    )


def with_history() -> Keyring:
    """A keyring that was rotated twice: one active key, two retired ones."""
    return Keyring.generate().rotated().rotation_completed().rotated().rotation_completed()


def virgin(tmp_path: Path, name: str) -> Settings:
    root = tmp_path / name
    return load_settings(
        {"SHELFHOLD_DATA_DIR": str(root / "data"), "SHELFHOLD_STORE_DIR": str(root / "store")}
    ).values


def no_leftover(settings: Settings) -> None:
    leftovers = [p.name for p in settings.data_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# -- the merge ------------------------------------------------------------------------------------


def test_secrets_import_merges_retired_kids_without_overwrite(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    exported = with_history()
    local = Keyring.generate()  # the instance has a key of its own
    diagnostics.boot_check(settings)
    store_of(settings).write(local)
    source = export_file(tmp_path, exported)

    assert import_cli(monkeypatch, settings, tmp_path, source) == 0

    merged = store_of(settings).load()
    assert merged.active.kid == local.active.kid  # the key in use is not replaced
    assert merged.active.material == local.active.material
    for key in exported.keys:  # every key of the export is kept, as a retired one, byte for byte
        kept = merged.get(key.kid)
        assert kept.material == key.material and kept.state == "retired"
    assert len(merged.keys) == len(exported.keys) + 1
    assert [k.state for k in merged.keys].count("active") == 1
    assert os.stat(store_of(settings).path).st_mode & 0o777 == 0o600
    no_leftover(settings)
    engine = create_db_engine(settings.database_path)
    try:  # the registry of the database says what the keyring says
        with engine.connect() as connection:
            kids = set(connection.execute(sa.select(secret_key.c.kid)).scalars())
    finally:
        engine.dispose()
    assert {k.kid for k in merged.keys} <= kids
    assert len(audits(settings)) == 1

    before = store_of(settings).path.read_bytes()  # again: nothing to add, nothing rewritten
    assert import_cli(monkeypatch, settings, tmp_path, source) == 0
    assert store_of(settings).path.read_bytes() == before


def test_secrets_import_conflicting_kid_refused(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    exported = with_history()
    retired = next(k for k in exported.keys if k.state == "retired")
    impostor = Key(kid=retired.kid, material=os.urandom(KEY_BYTES), state="retired")
    impostor = Key(
        kid=retired.kid,
        material=impostor.material,
        state="retired",
        retired_at=retired.retired_at,
    )
    local = Keyring(keys=(*Keyring.generate().keys, impostor))
    diagnostics.boot_check(settings)
    store_of(settings).write(local)
    before = store_of(settings).path.read_bytes()
    capsys.readouterr()

    assert import_cli(monkeypatch, settings, tmp_path, export_file(tmp_path, exported)) == 1

    captured = capsys.readouterr()
    assert retired.kid in captured.err  # the kid is said, never a byte of a key
    for key in (*exported.keys, impostor):
        assert base64.b64encode(key.material).decode() not in captured.out + captured.err
    assert store_of(settings).path.read_bytes() == before  # nothing was merged
    assert audits(settings) == []
    no_leftover(settings)


# -- the exit of tombstone_key_unknown ---------------------------------------------------------------


def test_secrets_import_exits_tombstone_key_unknown(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series("Purged")
        unit = env.add_chapter(series, "1")
        assert (
            run_cli(monkeypatch, settings, "purge", "--now", "--unit", unit, "--confirm", unit) == 0
        )
        original = store_of(settings).load()
        source = export_file(tmp_path, original)  # the export the admin kept
        engine = create_db_engine(settings.database_path)
        try:
            with engine.begin() as connection:  # the lines the journal holds and the database lost
                lines = connection.execute(
                    sa.select(sa.func.count()).select_from(coherence.content_tombstone)
                ).scalar_one()
                assert lines >= 1
                connection.execute(sa.delete(coherence.content_tombstone))
            store_of(settings).write(Keyring.generate())  # /data restored, the keyring was not

            waiting = waiting_states(status_json(monkeypatch, settings, capsys))
            command = waiting["tombstone_key_unknown"][0]["command"]
            assert command.startswith("shelfhold secrets import --passphrase-file")
            assert "accept-lost-keyring" not in command

            assert import_cli(monkeypatch, settings, tmp_path, source) == 0
            out = capsys.readouterr().out
            assert str(lines) in out  # "N ligne(s) rejouée(s)"

            assert "tombstone_key_unknown" not in waiting_states(
                status_json(monkeypatch, settings, capsys)
            )
            with engine.connect() as connection:  # the lines were really replayed from the journal
                replayed = connection.execute(
                    sa.select(sa.func.count()).select_from(coherence.content_tombstone)
                ).scalar_one()
                lost = connection.execute(
                    sa.select(sa.func.count()).select_from(coherence.tombstone_lost_kid)
                ).scalar_one()
            assert replayed == lines and lost == 0  # and nothing was waived on the way
        finally:
            engine.dispose()
        assert store_of(settings).load().find(original.active.kid) is not None


# -- who may ask -------------------------------------------------------------------------------------


def test_secrets_import_refused_to_api_key(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    exported = with_history()
    folder = settings.data_dir / "import"  # the one place the API reads from
    folder.mkdir()
    private(folder / "pass", PASSPHRASE + "\n")
    private(folder / "export.age", encrypt_export(exported, PASSPHRASE))
    body = {"passphrase_file": "pass", "in_file": "export.age"}
    with auth_env(monkeypatch, settings) as api:
        capsys.readouterr()
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci") == 0
        key = KEY.search(capsys.readouterr().out)
        assert key
        assert api.login().status_code == 200
        before = store_of(settings).path.read_bytes()

        refused = api.client.post(
            "/api/v1/secrets/import", json=body, headers={"Authorization": f"Bearer {key.group(0)}"}
        )  # an API key, whatever its scope
        assert refused.status_code == 403
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
        assert store_of(settings).path.read_bytes() == before and audits(settings) == []

        api.clock.advance(timedelta(minutes=20))  # a session that has not typed its password lately
        stale = api.post("/api/v1/secrets/import", json=body)
        assert stale.status_code == 401 and stale.json()["type"].endswith("reauth-required")
        assert store_of(settings).path.read_bytes() == before and audits(settings) == []

        assert api.login().status_code == 200  # fresh
        done = api.post("/api/v1/secrets/import", json=body)
        assert done.status_code == 200, done.text
        assert set(done.json()["added"]) == {k.kid for k in exported.keys}
        merged = store_of(settings).load()
        assert all(merged.find(k.kid) for k in exported.keys)
        rows = audits(settings)  # audited with who, and the session behind it
        assert len(rows) == 1 and rows[0][0] and rows[0][2]
        assert all(k.kid in str(rows[0][1]) for k in exported.keys)
        assert all(base64.b64encode(k.material).decode() not in str(rows) for k in exported.keys)
        assert PASSPHRASE not in str(db_rows(settings, "SELECT * FROM audit"))


def test_the_audit_is_written_in_the_transaction_of_the_registry(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """A registry that cannot be written leaves no audit row, and an audit that cannot be written
    leaves no registry row: they commit together."""
    migrate(settings)
    diagnostics.boot_check(settings)
    exported = with_history()
    source = export_file(tmp_path, exported)
    connection = sqlite3.connect(settings.database_path)
    try:
        connection.execute(
            "CREATE TRIGGER no_import_audit BEFORE INSERT ON audit "
            "WHEN NEW.action = 'secrets.import' BEGIN SELECT RAISE(ABORT, 'audit refused'); END"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(sa.exc.DBAPIError):
        import_cli(monkeypatch, settings, tmp_path, source)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as c:
            kids = set(c.execute(sa.select(secret_key.c.kid)).scalars())
    finally:
        engine.dispose()
    assert not {k.kid for k in exported.keys} & kids  # no registry row without its audit row


# -- an interruption --------------------------------------------------------------------------------


class Killed(BaseException):
    """What a SIGKILL is for the code that runs: nothing after it runs, nothing is caught."""


def test_secrets_import_interrupted_is_resumable(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series("Purged")
        unit = env.add_chapter(series, "1")
        assert (
            run_cli(monkeypatch, settings, "purge", "--now", "--unit", unit, "--confirm", unit) == 0
        )
        original = store_of(settings).load()
        source = export_file(tmp_path, original)
        engine = create_db_engine(settings.database_path)
        try:
            with engine.begin() as connection:
                connection.execute(sa.delete(coherence.content_tombstone))
            store_of(settings).write(Keyring.generate())

            def killed(*_args: object, **_kwargs: object) -> None:
                raise Killed

            with monkeypatch.context() as patch:
                patch.setattr(registry, "sync_registry", killed)  # after the keyring is replaced
                with pytest.raises(Killed):
                    import_cli(monkeypatch, settings, tmp_path, source)
            # the intention is durable (the keyring holds the keys), the work is not done
            assert store_of(settings).load().find(original.active.kid) is not None
            no_leftover(settings)
            assert audits(settings) == []
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.select(sa.func.count()).select_from(coherence.content_tombstone)
                    ).scalar_one()
                    == 0
                )

            capsys.readouterr()
            assert import_cli(monkeypatch, settings, tmp_path, source) == 0  # the same command
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        sa.select(sa.func.count()).select_from(coherence.content_tombstone)
                    ).scalar_one()
                    >= 1
                )
            assert len(audits(settings)) == 1
            assert "tombstone_key_unknown" not in waiting_states(
                status_json(monkeypatch, settings, capsys)
            )
        finally:
            engine.dispose()


@pytest.mark.parametrize("where", ["replace", "fsync_file"])
def test_the_keyring_is_never_half_written(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, where: str
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    before = store_of(settings).path.read_bytes()
    source = export_file(tmp_path, with_history())

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "synthetic I/O error")

    target = "os.replace" if where == "replace" else "os.fsync"
    with monkeypatch.context() as patch:
        patch.setattr(target, broken)
        assert import_cli(monkeypatch, settings, tmp_path, source) == 1
    assert store_of(settings).path.read_bytes() == before
    no_leftover(settings)
    assert audits(settings) == []


def test_the_keyring_is_synced_to_disk_before_it_replaces_the_old_one(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    assert import_cli(monkeypatch, settings, tmp_path, export_file(tmp_path, with_history())) == 0
    assert len(synced) >= 2  # the new file, then the folder that holds it


# -- forged files -----------------------------------------------------------------------------------


def forged_files(tmp_path: Path) -> dict[str, Path]:
    import pyrage

    good = encrypt_export(with_history(), PASSPHRASE)
    kid = "k-aaaaaaaaaaaaaaaa"

    def entry(material: bytes, state: str = "active", key_id: str = kid) -> dict[str, Any]:
        return {
            "kid": key_id,
            "alg": "xchacha20poly1305",
            "state": state,
            "key": base64.b64encode(material).decode(),
            "created_at": "2026-11-07T09:00:00+00:00",
            "retired_at": None if state == "active" else "2026-11-07T09:00:00+00:00",
        }

    def sealed(document: object) -> str:
        return armor(pyrage.passphrase.encrypt(json.dumps(document).encode(), PASSPHRASE))

    def doc(*keys: dict[str, Any]) -> dict[str, Any]:
        return {"format": 1, "keys": list(keys)}

    binary = pyrage.passphrase.encrypt(b"x" * 50, PASSPHRASE)
    greedy = binary.replace(b"scrypt", b"scrypt", 1)
    head, rest = greedy.split(b"\n---", 1)
    lines = head.split(b"\n")
    lines[1] = b" ".join([*lines[1].split(b" ")[:3], b"23"])
    contents = {
        "scrypt cost 2**23": armor(b"\n".join(lines) + b"\n---" + rest),
        "truncated": good[: len(good) // 2],
        "wrong header": good.replace("BEGIN AGE ENCRYPTED FILE", "BEGIN SOMETHING ELSE", 1),
        "duplicate kid": sealed(doc(entry(b"\1" * KEY_BYTES), entry(b"\2" * KEY_BYTES, "retired"))),
        "key of the wrong size": sealed(doc(entry(b"\1" * 31))),
        "giant json": armor(pyrage.passphrase.encrypt(b" " * (3 * 1024 * 1024), PASSPHRASE)),
        "deeply nested json": armor(pyrage.passphrase.encrypt(b"[" * 200_000, PASSPHRASE)),
    }
    files = {}
    for label, text in contents.items():
        files[label] = private(tmp_path / f"{label.replace(' ', '_')}.age", text)
    return files


@pytest.fixture
def forged(tmp_path: Path) -> Iterator[dict[str, Path]]:
    folder = tmp_path / "forged"
    folder.mkdir()
    yield forged_files(folder)


@pytest.mark.parametrize(
    "label",
    [
        "truncated",
        "wrong header",
        "duplicate kid",
        "key of the wrong size",
        "giant json",
        "deeply nested json",
    ],
)
def test_a_forged_export_imports_nothing(
    label: str,
    forged: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    before = store_of(settings).path.read_bytes()
    capsys.readouterr()

    assert import_cli(monkeypatch, settings, tmp_path, forged[label]) == 1

    captured = capsys.readouterr()
    assert PASSPHRASE not in captured.out + captured.err
    assert "Traceback" not in captured.err and captured.err.strip()
    assert store_of(settings).path.read_bytes() == before
    assert audits(settings) == []
    no_leftover(settings)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            known = set(connection.execute(sa.select(secret_key.c.kid)).scalars())
    finally:
        engine.dispose()
    assert "k-aaaaaaaaaaaaaaaa" not in known


# -- the passphrase ---------------------------------------------------------------------------------


def test_the_passphrase_is_read_from_a_file_with_the_right_permissions(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    before = store_of(settings).path.read_bytes()
    source = export_file(tmp_path, with_history())
    open_to_all = passphrase_file(tmp_path, mode=0o644)
    capsys.readouterr()
    assert import_cli(monkeypatch, settings, tmp_path, source, passphrase=open_to_all) == 1
    err = capsys.readouterr().err
    assert "chmod 600" in err and PASSPHRASE not in err
    assert store_of(settings).path.read_bytes() == before
    with pytest.raises(SystemExit):  # there is no option that takes the passphrase itself
        run_cli(
            monkeypatch,
            settings,
            "secrets",
            "import",
            "--passphrase",
            PASSPHRASE,
            "--in",
            str(source),
        )
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, settings, "secrets", "import", "--passphrase-file", str(open_to_all))
    assert PASSPHRASE not in capsys.readouterr().err


def test_a_wrong_passphrase_imports_nothing_and_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    before = store_of(settings).path.read_bytes()
    source = export_file(tmp_path, with_history(), passphrase="another passphrase entirely")
    capsys.readouterr()
    assert import_cli(monkeypatch, settings, tmp_path, source) == 1
    captured = capsys.readouterr()
    assert PASSPHRASE not in captured.out + captured.err
    assert store_of(settings).path.read_bytes() == before and audits(settings) == []


# -- a virgin instance ------------------------------------------------------------------------------


def test_export_then_import_round_trip_on_a_virgin_instance(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(settings)
    keyring = diagnostics.boot_check(settings)
    store_of(settings).write(keyring.rotated().rotation_completed())
    original = store_of(settings).load()
    engine = create_db_engine(settings.database_path)
    try:  # a secret sealed under the key that is about to be retired by the next rotation
        with engine.begin() as connection:
            registry.sync_registry(connection, original)
            connection.execute(sa.insert(source_config).values(plugin_id="p"))
            SecretVault(original).seal_row(connection, "source_config", "p", b"s3cret")
    finally:
        engine.dispose()
    out = tmp_path / "keyring.age"
    assert (
        run_cli(
            monkeypatch,
            settings,
            "secrets",
            "export",
            "--passphrase-file",
            str(passphrase_file(tmp_path)),
            "--out",
            str(out),
        )
        == 0
    )

    other = virgin(tmp_path, "virgin")  # no keyring, no data
    migrate(other)
    capsys.readouterr()
    assert import_cli(monkeypatch, other, tmp_path, out) == 0
    restored = store_of(other).load()
    assert restored.keys == store_of(settings).load().keys  # every key, the active one included
    assert restored.active.kid == original.active.kid
    assert os.stat(store_of(other).path).st_mode & 0o777 == 0o600
    # the copy of /data opens with it
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            assert SecretVault(restored).open_row(connection, "source_config", "p") == b"s3cret"
    finally:
        engine.dispose()
    assert run_cli(monkeypatch, other, "secrets", "status", "--json") == 0
    codes = {w["code"] for w in json.loads(capsys.readouterr().out.splitlines()[-1])["warnings"]}
    assert "keyring_absent" not in codes
    # and the keyring cannot be replaced by an import over an unrelated one: that is a merge
    again = import_cli(monkeypatch, other, tmp_path, out)
    assert again == 0 and store_of(other).load().keys == restored.keys


def test_import_where_the_database_holds_keys_and_the_keyring_is_gone(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The case `boot_check` refuses to start on: sealed secrets and no keyring."""
    migrate(settings)
    keyring = diagnostics.boot_check(settings)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            registry.sync_registry(connection, keyring)
    finally:
        engine.dispose()
    source = export_file(tmp_path, keyring)
    store_of(settings).path.unlink()
    with pytest.raises(Exception, match="keyring"):
        diagnostics.boot_check(settings)
    assert import_cli(monkeypatch, settings, tmp_path, source) == 0
    assert diagnostics.boot_check(settings).keys == keyring.keys


def test_secrets_import_is_a_reserved_exit_of_the_catalogue() -> None:
    from shelfhold.identity.public import EXIT_ROUTES

    route = next(r for r in EXIT_ROUTES if r.command == "secrets import")
    assert route.reserved is not None and route.reserved.name == "secrets.import"
    assert route.waiting_state == "tombstone_key_unknown"


def test_secrets_import_over_the_socket_of_the_server_carries_paths_only(
    settings: Settings, tmp_path: Path
) -> None:
    """The running server applies the import: the request holds two paths, the passphrase is read
    from its file by the server, and the host CLI is the principal (it is allowed, never a key)."""
    from shelfhold.identity.public import local_auth
    from shelfhold.infrastructure.local_socket import RpcFailure, call
    from shelfhold.interfaces.local_cli import IdentityDispatcher
    from shelfhold.ops.wiring import open_ops
    from tests.local_socket_env import serving

    migrate(settings)
    diagnostics.boot_check(settings)
    exported = with_history()
    params = {
        "passphrase_file": str(passphrase_file(tmp_path)),
        "in_file": str(export_file(tmp_path, exported)),
    }
    engine = create_db_engine(settings.database_path)
    try:
        auth = local_auth(engine, settings)
        sock = tmp_path / "s.sock"
        with serving(sock, IdentityDispatcher(auth, ops=open_ops(settings, engine=engine))):
            for bad in ({}, {"in_file": params["in_file"]}, {**params, "in_file": ""}):
                with pytest.raises(RpcFailure):
                    call(sock, "ops.secrets_import", bad)
            with pytest.raises(RpcFailure, match="absolute"):
                call(sock, "ops.secrets_import", {**params, "in_file": "export.age"})
            answer = call(sock, "ops.secrets_import", params)
    finally:
        engine.dispose()
    assert isinstance(answer, dict) and set(answer["added"]) == {k.kid for k in exported.keys}
    assert PASSPHRASE not in json.dumps(answer)
    assert all(store_of(settings).load().find(k.kid) for k in exported.keys)
    rows = audits(settings)
    assert len(rows) == 1 and str(rows[0][0]).startswith("local_cli:")


def test_the_api_reads_only_plain_names_in_the_import_folder(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    diagnostics.boot_check(settings)
    exported = with_history()
    outside = tmp_path / "outside"
    outside.mkdir()
    private(outside / "pass", PASSPHRASE + "\n")
    private(outside / "export.age", encrypt_export(exported, PASSPHRASE))
    before = store_of(settings).path.read_bytes()
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200

        def ask(passphrase: str, source: str) -> Any:
            return api.post(
                "/api/v1/secrets/import", json={"passphrase_file": passphrase, "in_file": source}
            )

        assert ask("pass", "export.age").status_code == 422  # no import folder yet
        folder = settings.data_dir / "import"
        folder.mkdir()
        private(folder / "pass", PASSPHRASE + "\n")
        private(folder / "export.age", encrypt_export(exported, PASSPHRASE))
        for bad in (
            "../outside/export.age",
            str(outside / "export.age"),
            "sub/export.age",
            ".hidden",
            "..",
            "a b",
        ):
            assert ask("pass", bad).status_code == 422, bad
            assert ask(bad, "export.age").status_code == 422, bad
        (folder / "link.age").symlink_to(outside / "export.age")  # a link out of the folder
        assert ask("pass", "link.age").status_code == 422
        (folder / "pass-link").symlink_to(outside / "pass")
        assert ask("pass-link", "export.age").status_code == 422
        assert store_of(settings).path.read_bytes() == before
        assert ask("pass", "export.age").status_code == 200


class Killed2(BaseException):
    """A SIGKILL after the commit: nothing after it runs."""


def test_a_rerun_after_a_kill_between_the_commit_and_the_replay_audits_once(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.ops import keyring as ops_keyring

    migrate(settings)
    diagnostics.boot_check(settings)
    source = export_file(tmp_path, with_history())

    def killed(self: object) -> int:
        raise Killed2

    with monkeypatch.context() as patch:
        patch.setattr(ops_keyring.TombstoneKeyringExit, "_replay", killed)
        with pytest.raises(Killed2):
            import_cli(monkeypatch, settings, tmp_path, source)
    assert len(audits(settings)) == 1  # committed with the registry
    assert import_cli(monkeypatch, settings, tmp_path, source) == 0  # the rerun
    assert len(audits(settings)) == 1


def test_the_registry_never_lifts_a_compromised_mark(settings: Settings) -> None:
    migrate(settings)
    keyring = diagnostics.boot_check(settings)
    retired = keyring.rotated().rotation_completed()
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            registry.sync_registry(connection, retired)
            connection.execute(
                sa.update(secret_key)
                .where(secret_key.c.kid == keyring.active.kid)
                .values(state="compromised")
            )
            registry.sync_registry(connection, retired)  # a keyring copy that says "retired"
            row = connection.execute(
                sa.select(secret_key.c.state).where(secret_key.c.kid == keyring.active.kid)
            ).scalar_one()
    finally:
        engine.dispose()
    assert row == "compromised"
