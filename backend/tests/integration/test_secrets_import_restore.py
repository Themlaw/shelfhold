"""EF-BIB-053, EF-INT-026, ENF-SEC-005 (M1b L06, review): a backup from before a rotation.

The restored database is `restored_behind`, the keyring on disk is the old one, and the journal
holds lines sealed under the new key: the adoption is blocked by `key_unknown`. The way out is
`secrets import` itself, which therefore must work under the freeze (for its /data half), through
the real services, and leave the replay to the adoption.
"""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings, load_settings
from shelfhold.export.application.restore import AdoptionBlockedError
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.schema import identity
from shelfhold.infrastructure.secrets.export import encrypt_export
from shelfhold.library.tombstones import (
    FROZEN_LOST_KEYRING_PHRASE,
    LOST_KEYRING_PHRASE,
    ContentHash,
    LostKeyringConfirmationError,
    VariantRef,
)
from shelfhold.ops import OpsServices
from shelfhold.ops.keyring import TombstoneKeyringExit
from shelfhold.ops.restore import RestoreGuardExit
from tests.integration.test_restore_epoch import new_roots
from tests.restore_env import RestoreEnv, restore_env

pytestmark = pytest.mark.req("EF-BIB-053", "EF-INT-026", "EF-INT-023", "ENF-SEC-005")

HOST = Principal(user_id="1000", username="uid:1000", role="admin", credential=Credential.LOCAL_CLI)


def settings_of(env: RestoreEnv) -> Settings:
    data = env.ex.lib.data
    return load_settings(
        {"SHELFHOLD_DATA_DIR": str(data), "SHELFHOLD_STORE_DIR": str(data.parent / "store")}
    ).values


def ops_over(env: RestoreEnv) -> OpsServices:
    settings = settings_of(env)
    clock = env.ex.clock
    return OpsServices(
        env.engine,
        clock=clock,
        bus=env.bus,
        restore=RestoreGuardExit(env.engine, lambda: env.fsops, clock, env.bus),
        keyring=TombstoneKeyringExit(settings, env.engine, clock),
    )


def test_secrets_import_unblocks_the_adoption_of_a_pre_rotation_restore(tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    folder = tmp_path_factory.mktemp("import")
    passphrase = folder / "pass"
    passphrase.write_text("synthetic passphrase 12345\n", encoding="utf-8")
    passphrase.chmod(0o600)
    export = folder / "export.age"
    with restore_env() as env:
        old = env.keyring()
        env.add_chapters(3, 3)
        backup = env.backup()
        env.export()
        rotated = old.rotated(env.ex.clock.now())
        export.write_text(encrypt_export(rotated, "synthetic passphrase 12345"), encoding="ascii")
        source_key, remote, content = env.variant_ref("3")
        values = VariantRef(
            "suwayomi", source_key, remote, (ContentHash("content-hash@1", content),)
        )
        with env.sessions.open(str(env.library), str(env.ex.lib.store)):
            env.tombstones(rotated).record(values, new_roots(env), reason="purge_unit")
        env.restore(backup)  # the database and the keyring are from before the rotation
        env.guard().enter_restored_behind()
        blocked = env.adopter(service=env.tombstones())
        assert blocked.preview().blocked_roots  # key_unknown, as the status says
        with pytest.raises(AdoptionBlockedError):
            blocked.adopt(confirm=blocked.preview().fingerprint)

        ops = ops_over(env)
        assert ops.restored_behind()  # the freeze holds, and the import goes through it
        report = ops.secrets_import(
            passphrase_file=str(passphrase), in_file=str(export), principal=HOST
        )
        assert report["deferred"] is True and report["replayed"] == 0
        assert env.fsops.frozen  # nothing was thawed, no root was written
        assert env.keyring().find(rotated.active.kid) is not None

        service = (
            env.tombstones()
        )  # what `reconcile --adopt-after-restore` builds: the disk's keyring
        service.replay(new_roots(env))
        adopter = env.adopter(service=service)
        proposal = adopter.preview()
        assert proposal.blocked_roots == {}
        done = adopter.adopt(confirm=proposal.fingerprint)
        assert done.readopted == ()
        assert env.fsops.frozen and done.pending_deletions  # the deletion still waits


def test_accept_lost_keyring_is_the_freeze_exit_when_there_is_no_export(tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    """No export, a restore from before a rotation: the adoption is blocked by `key_unknown`.
    `accept-lost-keyring`, with the long phrase that says what it costs, is the way out."""
    with restore_env() as env:
        old = env.keyring()
        env.add_chapters(3, 3)
        backup = env.backup()
        env.export()
        rotated = old.rotated(env.ex.clock.now())
        source_key, remote, content = env.variant_ref("3")
        values = VariantRef(
            "suwayomi", source_key, remote, (ContentHash("content-hash@1", content),)
        )
        with env.sessions.open(str(env.library), str(env.ex.lib.store)):
            env.tombstones(rotated).record(values, new_roots(env), reason="purge_unit")
        env.restore(backup)
        env.guard().enter_restored_behind()
        blocked = env.adopter(service=env.tombstones())
        assert blocked.preview().blocked_roots

        ops = ops_over(env)
        assert ops.restored_behind()
        with pytest.raises(LostKeyringConfirmationError) as short:  # the short phrase is not enough
            ops.tombstone_accept_lost_keyring(confirmation=LOST_KEYRING_PHRASE, principal=HOST)
        assert FROZEN_LOST_KEYRING_PHRASE in str(short.value)
        assert env.keyring().find(rotated.active.kid) is None
        waived = ops.tombstone_accept_lost_keyring(
            confirmation=FROZEN_LOST_KEYRING_PHRASE, principal=HOST
        )
        assert waived and env.fsops.frozen  # the kid is abandoned, nothing was thawed
        from shelfhold.infrastructure.secrets import diagnostics

        where = SimpleNamespace(database_path=env.db_path)  # the database of this fixture
        codes = {w.code for w in diagnostics._tombstone_warnings(where, env.keyring())}  # type: ignore[arg-type]
        assert "tombstone_kids_lost" in codes  # durable in `status`
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(identity.audit.c.actor_id, identity.audit.c.target).where(
                    identity.audit.c.action == "tombstone.accept_lost_keyring"
                )
            ).one()
        assert row.actor_id == HOST.actor_id and waived[0] in row.target

        service = env.tombstones()  # what the adoption builds
        service.replay(new_roots(env))
        adopter = env.adopter(service=service)
        proposal = adopter.preview()
        assert proposal.blocked_roots == {}
        done = adopter.adopt(confirm=proposal.fingerprint)
        assert (
            len(done.readopted) == 1
        )  # the cost the phrase named: the purged file is adopted again
