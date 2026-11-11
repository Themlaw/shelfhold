# ruff: noqa: S106
"""EF-INT-015, EF-INT-026, EF-USR-003: a restore never brings back a credential revoked since.

The backup holds the credentials of its day. A key revoked after it would be live again in the
restored database, and a session too: both are revoked by the restore (they are made again in one
command), and a notice that outlasts every restart asks for the passkeys and the passwords changed
since the backup to be looked at, until the reserved `restore acknowledge`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.identity.domain.catalogue import EXIT_ROUTES, RESERVED_ACTIONS
from shelfhold.identity.public import CreatedApiKey, Credential, Principal, local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.restore_notice import NOTICE_FILE, RestoreNotice
from shelfhold.infrastructure.schema import identity
from shelfhold.ops.backup import restore_archive
from shelfhold.ops.errors import NotWaitingError
from shelfhold.ops.restore import RestoreCredentialsExit
from tests.backup_env import BackupBench, backup_bench
from tests.restore_env import restore_env

pytestmark = pytest.mark.req("EF-INT-015", "EF-INT-026", "EF-USR-003")

PASSWORD = "a long synthetic password 1234"  # noqa: S105
ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)


def _rows(database: Path, sql: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _seed(bench: BackupBench) -> tuple[CreatedApiKey, CreatedApiKey, str]:
    """An admin, one key that will be revoked after the backup, one that stays live, a session."""
    auth = local_auth(bench.engine, bench.settings, bench.clock)
    auth.create_admin("admin", PASSWORD)
    revoked_later = auth.create_api_key(name="later")
    kept = auth.create_api_key(name="kept")
    session = auth.login("admin", PASSWORD, ip="10.0.0.1")
    return revoked_later, kept, session.token


def test_restore_refuses_the_key_revoked_after_the_backup(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        key_a, key_b, token = _seed(bench)
        bench.service.run()
        archive = bench.backups / bench.names()[0]
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        assert auth.revoke_api_key(key_a.prefix)  # revoked after the backup
        assert auth.authenticate_api_key(key_a.value) is None

        target = tmp_path / "restored"
        report = restore_archive(
            archive, target, bench.passphrase, live_data_dir=bench.settings.data_dir
        )
        assert (report.sessions_revoked, report.api_keys_revoked) == (1, 2)

        engine = create_db_engine(target / "shelfhold.db")
        try:
            restored = local_auth(engine, bench.settings, bench.clock)
            assert restored.authenticate_api_key(key_a.value) is None  # the point of it all
            assert (
                restored.authenticate_api_key(key_b.value) is None
            )  # live in the backup: revoked too
            assert restored.authenticate_session(token) is None
            restored.login("admin", PASSWORD, ip="10.0.0.2")  # the account itself is intact
            audited = restored.recent_audit_targets("restore.credentials_revoked")
            assert audited == ["sessions=1 api_keys=2"]
            made_again = restored.create_api_key(name="again")  # they can be made again
            assert restored.authenticate_api_key(made_again.value) is not None
        finally:
            engine.dispose()


def test_the_restore_leaves_a_notice_that_status_lists_until_it_is_acknowledged(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path) as bench:
        _seed(bench)
        bench.service.run()
        target = tmp_path / "restored"
        restore_archive(
            bench.backups / bench.names()[0], target, bench.passphrase,
            live_data_dir=bench.settings.data_dir,
        )  # fmt: skip
        assert (target / NOTICE_FILE).is_file()
        assert (target / NOTICE_FILE).stat().st_mode & 0o077 == 0
        engine = create_db_engine(target / "shelfhold.db")
        try:
            for _restart in range(2):  # a restart changes nothing: the file is the state
                exit_ = RestoreCredentialsExit(target, engine, bench.clock)
                (item,) = exit_.waiting()
                assert item.state == "restored_credentials"
                assert "passkeys" in item.reason
            exit_.acknowledge(principal=ADMIN)
            assert exit_.waiting() == []
            with pytest.raises(NotWaitingError):
                exit_.acknowledge(principal=ADMIN)
            with engine.connect() as connection:
                actions = connection.execute(sa.select(identity.audit.c.action)).scalars().all()
            assert "restore.credentials_acknowledged" in actions
            assert "restore.credentials_revoked" in actions  # the revocation stays in the audit
        finally:
            engine.dispose()


def test_the_acknowledgement_is_a_reserved_exit_of_its_state() -> None:
    routes = [r for r in EXIT_ROUTES if r.waiting_state == "restored_credentials"]
    assert [(r.command, r.reserved) for r in routes] == [
        ("restore acknowledge", RESERVED_ACTIONS["restore.acknowledge"])
    ]


def test_a_start_behind_its_disk_revokes_every_credential_and_notes_it() -> None:
    from tests.integration.test_restore_epoch import restored

    with restore_env() as env:
        now = env.ex.clock.now()
        with env.engine.begin() as connection:
            user = "01JAAAAAAAAAAAAAAAAAAAAAAA"
            connection.execute(
                sa.insert(identity.user).values(
                    id=user,
                    username="admin",
                    role="admin",
                    password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA",
                    created_at=now,
                    can_rate=True,
                )
            )
            connection.execute(
                sa.insert(identity.api_key).values(
                    id="01JBBBBBBBBBBBBBBBBBBBBBBB",
                    user_id=user,
                    prefix="shk_zzzzzzzz",
                    hash="sha256:" + "0" * 64,
                    scopes=["admin"],
                    created_at=now,
                )
            )
        restored(env)  # the backup holds the live key; the disk moved on
        status = env.guard().enter_restored_behind()
        assert status.behind
        assert status.revoked == (0, 1)
        with env.engine.connect() as connection:
            revoked = connection.execute(sa.select(identity.api_key.c.revoked_at)).scalar()
        assert revoked is not None
        again = env.guard().enter_restored_behind()  # the second start counts nothing new
        assert again.revoked is None


def test_the_notice_survives_and_an_unreadable_one_still_warns(tmp_path: Path) -> None:
    notice = RestoreNotice(tmp_path)
    assert notice.read() is None
    (tmp_path / NOTICE_FILE).write_text("not json", encoding="utf-8")
    assert notice.read() is not None  # a damaged notice is still a notice
    assert notice.acknowledge() is True
    assert notice.acknowledge() is False
