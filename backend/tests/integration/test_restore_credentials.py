# ruff: noqa: S106
"""EF-INT-015, EF-INT-026, EF-USR-003: a restore never brings back a credential revoked since.

The backup holds the credentials of its day. A key revoked after it would be live again in the
restored database, and a session too: both are revoked by the restore (they are made again in one
command). Three ways in, one outcome: `backup restore`, a database put back by hand (the
identity counter of /data is ahead of the database), and a library found ahead of the database
(`restored_behind`). The notice is a row of the database; until `restore acknowledge`, `status`
lists it and no passkey signs in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from shelfhold.identity.application.passkeys import PasskeyUnavailableError
from shelfhold.identity.domain.catalogue import EXIT_ROUTES, RESERVED_ACTIONS
from shelfhold.identity.public import (
    AuthError,
    CreatedApiKey,
    Credential,
    PasswordLoginSuspendedError,
    Principal,
    local_auth,
    reconcile_identity_counter,
    revoke_all_credentials,
)
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.identity_counter import (
    COUNTER_FILE,
    IdentityCounter,
    IdentityCounterError,
)
from shelfhold.infrastructure.restore_notice import notice_active, read_notice
from shelfhold.infrastructure.schema import identity
from shelfhold.ops.backup import restore_archive
from shelfhold.ops.errors import NotWaitingError
from shelfhold.ops.restore import RestoreCredentialsExit
from tests.backup_env import BackupBench, backup_bench, read_members
from tests.restore_env import restore_env

pytestmark = pytest.mark.req("EF-INT-015", "EF-INT-026", "EF-USR-003")

PASSWORD = "a long synthetic password 1234"  # noqa: S105
NEW_PASSWORD = "another long synthetic password 5678"  # noqa: S105
ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)
PASSKEY_A, PASSKEY_B = "01JCCCCCCCCCCCCCCCCCCCCCCC", "01JDDDDDDDDDDDDDDDDDDDDDDD"


def _seed(bench: BackupBench) -> tuple[CreatedApiKey, CreatedApiKey, str]:
    """An admin, one key that will be revoked after the backup, one that stays live, a session."""
    auth = local_auth(bench.engine, bench.settings, bench.clock)
    auth.create_admin("admin", PASSWORD)
    revoked_later = auth.create_api_key(name="later")
    kept = auth.create_api_key(name="kept")
    session = auth.login("admin", PASSWORD, ip="10.0.0.1")
    return revoked_later, kept, session.token


def _add_passkey(bench: BackupBench, key_id: str, name: str, *, created: object) -> None:
    with bench.engine.begin() as connection:
        (user_id,) = connection.execute(sa.select(identity.user.c.id)).one()
        connection.execute(
            sa.insert(identity.webauthn_credential).values(
                id=key_id,
                user_id=user_id,
                credential_id=name.encode(),
                public_key=b"k",
                name=name,
                created_at=created,
            )
        )


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
            restored = local_auth(engine, bench.settings.model_copy(update={"data_dir": target}))
            assert restored.authenticate_api_key(key_a.value) is None  # the point of it all
            assert restored.authenticate_api_key(key_b.value) is None  # live in the backup too
            assert restored.authenticate_session(token) is None
            restored.login("admin", PASSWORD, ip="10.0.0.2")  # the account itself is intact
            assert restored.recent_audit_targets("restore.credentials_revoked") == [
                "sessions=1 api_keys=2"
            ]
            made_again = restored.create_api_key(name="again")  # they can be made again
            assert restored.authenticate_api_key(made_again.value) is not None
        finally:
            engine.dispose()


def test_a_database_put_back_by_hand_is_caught_by_the_identity_counter(tmp_path: Path) -> None:
    """No library, no sentinel to be behind: the file of /data is ahead of the database."""
    with backup_bench(tmp_path) as bench:
        key, _kept, token = _seed(bench)
        data = bench.settings.data_dir
        hand_made = tmp_path / "hand-made.db"
        with sqlite3.connect(bench.settings.database_path) as live, sqlite3.connect(hand_made) as c:
            live.backup(c)  # `cp backup.db /data/shelfhold.db`, taken now
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        assert auth.revoke_api_key(key.prefix)  # closed after the copy was made
        assert auth.revoke_sessions(username="admin") >= 1
        on_file = IdentityCounter(data).file_value()
        assert on_file is not None and on_file >= 2
        bench.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            Path(str(bench.settings.database_path) + suffix).unlink(missing_ok=True)
        hand_made.replace(bench.settings.database_path)

        engine = create_db_engine(bench.settings.database_path)
        try:
            restored = local_auth(engine, bench.settings, bench.clock)
            assert restored.authenticate_api_key(key.value) is not None  # live again: the hole
            assert restored.authenticate_session(token) is not None
            acted = reconcile_identity_counter(
                engine, IdentityCounter(data), now=bench.clock.now()
            )  # what every start runs, before any request
            assert acted == (1, 2)
            assert restored.authenticate_api_key(key.value) is None
            assert restored.authenticate_session(token) is None
            with engine.connect() as connection:
                assert notice_active(connection)
                assert IdentityCounter.db_value(connection) == IdentityCounter(data).file_value()
            with pytest.raises(PasskeyUnavailableError) as refused:
                restored.passkey_login_options(ip="10.0.0.3")
            assert refused.value.reason == "restore_pending"
            assert (
                reconcile_identity_counter(  # a second start: nothing new
                    engine, IdentityCounter(data), now=bench.clock.now()
                )
                is None
            )
        finally:
            engine.dispose()


def test_every_revocation_and_password_change_moves_the_counter_in_file_and_database(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, _token = _seed(bench)
        counter = IdentityCounter(bench.settings.data_dir)
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        seen = [counter.file_value() or 0]
        for change in (
            lambda: auth.revoke_api_key(key.prefix),
            lambda: auth.revoke_sessions(username="admin"),
            lambda: auth.reset_password("admin", NEW_PASSWORD),
        ):
            change()
            seen.append(counter.file_value() or 0)
            with bench.engine.connect() as connection:
                assert IdentityCounter.db_value(connection) == seen[-1]
        assert seen == sorted(set(seen)) and len(seen) == 4  # strictly increasing
        assert not list(bench.settings.data_dir.glob("*.tmp"))  # no fixed-name temporary left
        bench.service.run()
        assert COUNTER_FILE not in "".join(read_members(bench.backups / bench.names()[0]))


def test_the_notice_and_the_revocation_commit_or_fail_together(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, _token = _seed(bench)
        counter = IdentityCounter(bench.settings.data_dir)
        before = counter.file_value() or 0
        with pytest.raises(RuntimeError), bench.engine.begin() as connection:
            revoke_all_credentials(connection, now=bench.clock.now(), counter=counter)
            raise RuntimeError("crash before the commit")
        with bench.engine.connect() as connection:
            assert read_notice(connection) is None  # nothing half done
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        assert auth.authenticate_api_key(key.value) is not None
        assert (counter.file_value() or 0) >= (before or 0)  # never behind: the safe side


def test_the_restore_leaves_a_notice_in_the_database_until_it_is_acknowledged(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path) as bench:
        _seed(bench)
        bench.service.run()
        target = tmp_path / "restored"
        restore_archive(
            bench.backups / bench.names()[0],
            target,
            bench.passphrase,
            live_data_dir=bench.settings.data_dir,
        )
        engine = create_db_engine(target / "shelfhold.db")
        try:
            auth = local_auth(engine, bench.settings.model_copy(update={"data_dir": target}))
            with pytest.raises(PasskeyUnavailableError) as refused:
                auth.passkey_login_options(ip="10.0.0.4")
            assert refused.value.reason == "restore_pending"
            for _restart in range(2):  # a restart changes nothing: the row is the state
                exit_ = RestoreCredentialsExit(engine, bench.clock)
                (item,) = exit_.waiting()
                assert item.state == "restored_credentials"
                assert "passkey" in item.reason
            assert exit_.acknowledge(principal=ADMIN) == []
            assert exit_.waiting() == []
            with pytest.raises(NotWaitingError):
                exit_.acknowledge(principal=ADMIN)
            with pytest.raises(PasskeyUnavailableError) as open_again:
                auth.passkey_login_options(ip="10.0.0.5")
            assert open_again.value.reason != "restore_pending"
            with engine.connect() as connection:
                actions = connection.execute(sa.select(identity.audit.c.action)).scalars().all()
            assert "restore.credentials_acknowledged" in actions
            assert "restore.credentials_revoked" in actions  # the revocation stays in the audit
        finally:
            engine.dispose()


def test_the_live_database_gives_its_revocations_and_passwords_to_the_restored_one(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path) as bench:
        _seed(bench)
        _add_passkey(bench, PASSKEY_A, "laptop", created=bench.clock.now())
        bench.service.run()
        archive = bench.backups / bench.names()[0]
        later = bench.clock.now() + timedelta(hours=2)
        _add_passkey(bench, PASSKEY_B, "phone", created=later)  # made after the backup
        with bench.engine.begin() as connection:  # and the first one retired after it
            connection.execute(
                sa.update(identity.webauthn_credential)
                .where(identity.webauthn_credential.c.id == PASSKEY_A)
                .values(revoked_at=later)
            )
            live_hash = connection.execute(sa.select(identity.user.c.password_hash)).scalar_one()
        for live_dir, carried in ((bench.settings.data_dir, True), (None, False)):
            target = tmp_path / f"restored-{carried}"
            restore_archive(archive, target, bench.passphrase, live_data_dir=live_dir)
            engine = create_db_engine(target / "shelfhold.db")
            try:
                with engine.connect() as connection:
                    revoked = connection.execute(
                        sa.select(identity.webauthn_credential.c.revoked_at)
                    ).scalar_one()
                    password = connection.execute(sa.select(identity.user.c.password_hash)).scalar()
                    notice = read_notice(connection)
                assert notice is not None and notice.passwords_carried is carried
                assert (revoked is not None) is carried  # carried from the live database, or not
                if carried:
                    assert sorted(notice.passkey_changes) == ["created: phone", "revoked: laptop"]
                else:
                    assert notice.passkey_changes == ()
                    assert password != "" and (password == live_hash)  # unchanged in both here
            finally:
                engine.dispose()


def test_a_reset_password_after_the_backup_is_carried_over(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        _seed(bench)
        bench.service.run()
        local_auth(bench.engine, bench.settings, bench.clock).reset_password("admin", NEW_PASSWORD)
        target = tmp_path / "restored"
        restore_archive(
            bench.backups / bench.names()[0],
            target,
            bench.passphrase,
            live_data_dir=bench.settings.data_dir,
        )
        engine = create_db_engine(target / "shelfhold.db")
        try:
            restored = local_auth(engine, bench.settings.model_copy(update={"data_dir": target}))
            restored.login("admin", NEW_PASSWORD, ip="10.0.0.6")  # not annulled by the restore
        finally:
            engine.dispose()
        other = tmp_path / "restored-blind"
        restore_archive(bench.backups / bench.names()[0], other, bench.passphrase)  # no live data
        engine = create_db_engine(other / "shelfhold.db")
        try:
            blind = local_auth(engine, bench.settings.model_copy(update={"data_dir": other}))
            with pytest.raises(PasswordLoginSuspendedError):  # the old one is back: none signs in
                blind.login("admin", PASSWORD, ip="10.0.0.7")
            with engine.connect() as connection:
                notice = read_notice(connection)
            assert notice is not None and notice.passwords_carried is False
        finally:
            engine.dispose()


def test_the_acknowledgement_is_a_reserved_exit_of_the_host_cli_only() -> None:
    from datetime import UTC, datetime

    from shelfhold.identity.public import Allowed, CredentialNotAllowed, authorize_reserved

    routes = [r for r in EXIT_ROUTES if r.waiting_state == "restored_credentials"]
    assert [(r.command, r.reserved) for r in routes] == [
        ("restore acknowledge", RESERVED_ACTIONS["restore.acknowledge"])
    ]
    now = datetime(2026, 11, 11, tzinfo=UTC)
    action = RESERVED_ACTIONS["restore.acknowledge"]
    fresh = Principal(
        "u", "admin", "admin", Credential.SESSION, session_id="s", auth_time=now, can_rate=True
    )
    for who in (fresh, replace(fresh, credential=Credential.API_KEY, key_id="k")):
        verdict = authorize_reserved(who, action, now=now, fresh_auth_s=900)
        assert isinstance(verdict, CredentialNotAllowed)  # a leaked password cannot lift it
    host = replace(fresh, credential=Credential.LOCAL_CLI)
    assert isinstance(authorize_reserved(host, action, now=now), Allowed)


def test_a_start_behind_its_disk_revokes_every_credential() -> None:
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
                    created_at=now,
                    can_rate=True,
                    password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2hoYXNoaGFzaA",
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
            assert notice_active(connection)  # written in the same transaction
        assert env.guard().enter_restored_behind().revoked is None  # the second start: nothing


def _hand_copy(bench: BackupBench, tmp_path: Path) -> Path:
    copy = tmp_path / "hand-made.db"
    with sqlite3.connect(bench.settings.database_path) as live, sqlite3.connect(copy) as target:
        live.backup(target)
    return copy


def _put_back(bench: BackupBench, copy: Path) -> None:
    bench.engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(str(bench.settings.database_path) + suffix).unlink(missing_ok=True)
    copy.replace(bench.settings.database_path)


def test_a_command_run_on_the_copied_database_before_the_start_does_not_hide_the_gap(
    tmp_path: Path,
) -> None:
    """A revocation made by the CLI on the copy brings the counter of the database to the file's:
    the start would see nothing. Every write of identity first revokes what the copy brings back."""
    with backup_bench(tmp_path) as bench:
        key, _kept, token = _seed(bench)
        copy = _hand_copy(bench, tmp_path)
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        assert auth.revoke_api_key(key.prefix)
        _put_back(bench, copy)
        engine = create_db_engine(bench.settings.database_path)
        try:
            cli = local_auth(engine, bench.settings, bench.clock)
            other = cli.create_api_key(name="tmp")
            cli.revoke_api_key(other.prefix)  # any revocation, before the start
            assert cli.authenticate_api_key(key.value) is None
            assert cli.authenticate_session(token) is None
            with engine.connect() as connection:
                assert notice_active(connection)
            assert (
                reconcile_identity_counter(
                    engine, IdentityCounter(bench.settings.data_dir), now=bench.clock.now()
                )
                is None
            )  # nothing left to do at start
        finally:
            engine.dispose()


def test_a_password_rehashed_at_login_on_the_copy_does_not_hide_the_gap(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, _token = _seed(bench)
        copy = _hand_copy(bench, tmp_path)
        _put_back(bench, copy)  # the file is ahead: something was revoked after the copy
        IdentityCounter(bench.settings.data_dir).publish(50)
        engine = create_db_engine(bench.settings.database_path)
        try:
            store_auth = local_auth(engine, bench.settings, bench.clock)
            user = store_auth._store.user_by_name("admin")
            assert user is not None
            store_auth._store.replace_password_hash(user.id, user.password_hash, user.password_hash)
            assert store_auth.authenticate_api_key(key.value) is None
        finally:
            engine.dispose()


def test_a_leaked_password_cannot_sign_in_or_lift_the_passkey_suspension(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        auth, _key, _token = (local_auth(bench.engine, bench.settings, bench.clock), None, None)
        auth.create_admin("admin", PASSWORD)
        copy = _hand_copy(bench, tmp_path)
        auth.reset_password("admin", NEW_PASSWORD)  # the old one leaked
        _put_back(bench, copy)
        engine = create_db_engine(bench.settings.database_path)
        try:
            reconcile_identity_counter(
                engine, IdentityCounter(bench.settings.data_dir), now=bench.clock.now()
            )
            restored = local_auth(engine, bench.settings, bench.clock)
            with pytest.raises(PasswordLoginSuspendedError):
                restored.login("admin", PASSWORD, ip="10.6.6.6")  # the leaked password
            with pytest.raises(PasswordLoginSuspendedError):
                restored.login("admin", NEW_PASSWORD, ip="10.6.6.7")  # not even a good one
            with pytest.raises(PasskeyUnavailableError):
                restored.passkey_reauth_options(ADMIN)  # nor a passkey proof of a session
            from_host = RestoreCredentialsExit(engine, bench.clock)
            (item,) = from_host.waiting()
            assert "reset-password" in item.reason  # how the admin gets back in
            restored.reset_password("admin", NEW_PASSWORD)  # from the host
            from_host.acknowledge(principal=ADMIN)  # from the host
            restored.login("admin", NEW_PASSWORD, ip="10.0.0.8")
        finally:
            engine.dispose()


def test_an_account_deleted_since_the_backup_comes_back_without_password_or_passkeys(
    tmp_path: Path,
) -> None:
    from shelfhold.identity.adapters.passwords import Argon2Hasher

    member_password = "member synthetic " + "x" * 12
    with backup_bench(tmp_path) as bench:
        auth, _key, _token = (local_auth(bench.engine, bench.settings, bench.clock), None, None)
        auth.create_admin("admin", PASSWORD)
        with bench.engine.begin() as connection:
            connection.execute(
                sa.insert(identity.user).values(
                    id="01JMEMBER00000000000000000", username="bob", role="member",
                    can_rate=False, password_hash=Argon2Hasher().hash(member_password),
                    created_at=bench.clock.now(),
                )
            )  # fmt: skip
        bench.service.run()
        auth.delete_user("bob")
        target = tmp_path / "restored"
        restore_archive(
            bench.backups / bench.names()[0], target, bench.passphrase,
            live_data_dir=bench.settings.data_dir,
        )  # fmt: skip
        engine = create_db_engine(target / "shelfhold.db")
        try:
            with engine.connect() as connection:
                notice = read_notice(connection)
                query = sa.select(identity.user.c.username, identity.user.c.password_hash)
                hashes = {name: value for name, value in connection.execute(query).all()}
            assert notice is not None and notice.deleted_accounts == ("bob",)
            assert hashes["bob"] is None and hashes["admin"] is not None
            restored = local_auth(engine, bench.settings.model_copy(update={"data_dir": target}))
            with pytest.raises(AuthError):
                restored.login("bob", member_password, ip="10.0.0.9", confirm_remember=True)
            (item,) = RestoreCredentialsExit(engine, bench.clock).waiting()
            assert "bob" in item.reason
        finally:
            engine.dispose()


def test_a_passkey_only_account_keeps_no_hash_not_the_text_none(tmp_path: Path) -> None:
    from shelfhold.identity.adapters.passwords import Argon2Hasher

    with backup_bench(tmp_path) as bench:
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        auth.create_admin("admin", PASSWORD)
        with bench.engine.begin() as connection:
            connection.execute(
                sa.insert(identity.user).values(
                    id="01JMEMBER00000000000000000", username="bob", role="member",
                    can_rate=False, password_hash=Argon2Hasher().hash("m" * 20),
                    created_at=bench.clock.now(),
                )
            )  # fmt: skip
        bench.service.run()
        with bench.engine.begin() as connection:  # bob became passkey-only in the live database
            connection.execute(
                sa.update(identity.user)
                .where(identity.user.c.username == "bob")
                .values(password_hash=None)
            )
        target = tmp_path / "restored"
        restore_archive(
            bench.backups / bench.names()[0], target, bench.passphrase,
            live_data_dir=bench.settings.data_dir,
        )  # fmt: skip
        with sqlite3.connect(target / "shelfhold.db") as connection:
            row = connection.execute("SELECT password_hash FROM user WHERE username='bob'")
            assert row.fetchone() == (None,)


def _kill_after_file_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process dies right after the file got `pending`, before the database commits."""
    real = IdentityCounter._write

    def killed(self: IdentityCounter, committed: int, pending: int | None) -> None:
        real(self, committed, pending)
        raise SystemExit("SIGKILL")

    monkeypatch.setattr(IdentityCounter, "_write", killed)


def test_a_crash_before_the_commit_of_a_password_change_is_not_a_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No restore at all: the write never committed. Nothing is revoked at the next start."""
    with backup_bench(tmp_path) as bench:
        key, _kept, token = _seed(bench)
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        session = SimpleNamespace(token=token)
        counter = IdentityCounter(bench.settings.data_dir)
        before = counter.file_value() or 0
        _kill_after_file_write(monkeypatch)
        with pytest.raises(SystemExit):
            auth.reset_password("admin", NEW_PASSWORD)
        monkeypatch.undo()
        committed, pending = counter.state()
        assert committed == before and pending == before + 1  # the two phases
        assert reconcile_identity_counter(bench.engine, counter, now=bench.clock.now()) is None
        again = local_auth(bench.engine, bench.settings, bench.clock)
        assert again.authenticate_session(session.token) is not None
        assert again.authenticate_api_key(key.value) is not None
        again.login("admin", PASSWORD, ip="10.0.0.1")  # the old password still stands
        with bench.engine.connect() as connection:
            assert not notice_active(connection)
        assert counter.state() == (before, None)  # the pending is dropped


def test_a_failed_commit_leaves_the_database_number_and_drops_the_pending(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path) as bench:
        _seed(bench)
        counter = IdentityCounter(bench.settings.data_dir)
        with bench.engine.connect() as connection:
            number = IdentityCounter.db_value(connection)
        with pytest.raises(RuntimeError), bench.engine.begin() as connection:
            counter.bump(connection)
            raise RuntimeError("the commit failed: disk full")
        with bench.engine.connect() as connection:
            assert IdentityCounter.db_value(connection) == number  # D stays at N
        assert reconcile_identity_counter(bench.engine, counter, now=bench.clock.now()) is None
        assert counter.state() == (number, None)


def test_a_crash_after_the_commit_is_adopted_not_revoked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, _token = _seed(bench)
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        counter = IdentityCounter(bench.settings.data_dir)
        # M3 L07 (review M1): a reset closes the keys of ITS account, so the reset that crashes is
        # another profile's; the point stays that adopting the committed counter revokes nothing
        auth.profile_add("mum", PASSWORD, "member")

        def killed(_self: IdentityCounter, _value: int) -> None:
            raise SystemExit("SIGKILL after the commit")

        monkeypatch.setattr(IdentityCounter, "publish", killed)
        with pytest.raises(SystemExit):
            auth.reset_password("mum", NEW_PASSWORD)
        monkeypatch.undo()
        with bench.engine.connect() as connection:
            number = IdentityCounter.db_value(connection)
        committed, pending = counter.state()
        assert pending == number and (committed or 0) < number  # the database did commit
        assert reconcile_identity_counter(bench.engine, counter, now=bench.clock.now()) is None
        assert counter.state() == (number, None)  # adopted
        assert (
            local_auth(bench.engine, bench.settings, bench.clock).authenticate_api_key(key.value)
            is not None
        )


def test_an_older_database_put_back_while_a_pending_exists_still_revokes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, token = _seed(bench)
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        copy = _hand_copy(bench, tmp_path)
        auth.revoke_api_key(auth.create_api_key(name="x").prefix)  # the counter moves on
        _kill_after_file_write(monkeypatch)
        with pytest.raises(SystemExit):
            auth.reset_password("admin", NEW_PASSWORD)  # and a pending is left
        monkeypatch.undo()
        assert IdentityCounter(bench.settings.data_dir).state()[1] is not None
        _put_back(bench, copy)
        engine = create_db_engine(bench.settings.database_path)
        try:
            acted = reconcile_identity_counter(
                engine, IdentityCounter(bench.settings.data_dir), now=bench.clock.now()
            )
            assert acted is not None  # committed is above the old database: a restore
            restored = local_auth(engine, bench.settings, bench.clock)
            assert restored.authenticate_api_key(key.value) is None
            assert restored.authenticate_session(token) is None
        finally:
            engine.dispose()


def test_the_old_counter_file_format_is_migrated_without_a_revocation(tmp_path: Path) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, token = _seed(bench)
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        counter = IdentityCounter(bench.settings.data_dir)
        with bench.engine.connect() as connection:
            number = IdentityCounter.db_value(connection)
        counter.path.write_bytes(str(number).encode())  # what the first version wrote
        assert counter.state() == (number, None)
        assert reconcile_identity_counter(bench.engine, counter, now=bench.clock.now()) is None
        counter.path.write_bytes(str(number + 5).encode())  # an old file ahead: a restore
        assert reconcile_identity_counter(bench.engine, counter, now=bench.clock.now()) is not None
        assert auth.authenticate_api_key(key.value) is None
        assert auth.authenticate_session(token) is None
        committed, pending = counter.state()
        assert pending is None and (committed or 0) > number + 5  # rewritten in the new format
        assert counter.path.read_text().startswith("committed=")


def test_a_database_error_at_start_is_retried_then_stops_the_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import time

    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(time, "sleep", lambda _s: None)

    class Locked:
        calls = 0

        def begin(self) -> object:
            Locked.calls += 1
            raise OperationalError("BEGIN", {}, Exception("database is locked"))

    with backup_bench(tmp_path) as bench:
        with pytest.raises(OperationalError):  # not skipped: the start is refused
            reconcile_identity_counter(
                Locked(),  # type: ignore[arg-type]
                IdentityCounter(bench.settings.data_dir),
                now=bench.clock.now(),
            )
        assert Locked.calls == 5  # tried again before giving up

        class NoTable(Locked):
            def begin(self) -> object:
                raise OperationalError("SELECT", {}, Exception("no such table: app_setting"))

        assert (
            reconcile_identity_counter(
                NoTable(),  # type: ignore[arg-type]
                IdentityCounter(bench.settings.data_dir),
                now=bench.clock.now(),
            )
            is None
        )  # not migrated yet: nothing to compare


@pytest.mark.parametrize("content", [b"", b"garbage", b"\xff\xfe"])
def test_an_empty_or_damaged_counter_file_revokes_once_and_is_written_again(
    tmp_path: Path, content: bytes
) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, _token = _seed(bench)
        counter = IdentityCounter(bench.settings.data_dir)
        counter.path.write_bytes(content)
        first = reconcile_identity_counter(bench.engine, counter, now=bench.clock.now())
        assert first is not None  # an empty file is a torn write, not a zero
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        assert auth.authenticate_api_key(key.value) is None
        again = reconcile_identity_counter(bench.engine, counter, now=bench.clock.now())
        assert again is None  # one revocation, not one per start
        assert (counter.file_value() or 0) > 0


def test_a_directory_in_place_of_the_counter_refuses_the_start_without_revoking(
    tmp_path: Path,
) -> None:
    with backup_bench(tmp_path) as bench:
        key, _kept, _token = _seed(bench)
        counter = IdentityCounter(bench.settings.data_dir)
        counter.path.unlink(missing_ok=True)
        counter.path.mkdir()
        for _start in range(2):
            with pytest.raises(IdentityCounterError, match="is a directory"):
                reconcile_identity_counter(bench.engine, counter, now=bench.clock.now())
        auth = local_auth(bench.engine, bench.settings, bench.clock)
        assert auth.authenticate_api_key(key.value) is not None  # nothing was revoked by it
