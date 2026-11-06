"""Contradictor review of M1a, blockers 2 and 3: each test is a proof of concept, turned around.

- an empty journal is a journal that was never given its header: it reads as none;
- a journal that cannot be read blocks its root, and a reserved exit puts it aside (never deletes
  it) and writes it again from the database;
- lines owed to a root that was away are paid when the root is back, without a restart.
"""

import io
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.schema import coherence, platform
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.library.tombstones import SetAsideRefusedError, VariantRef
from shelfhold.library.tombstones.service import LostKeyringRefusedError, TombstoneRoot
from shelfhold.ops.errors import ExitUnavailableError
from tests.intents_env import NOW
from tests.l41_env import instance
from tests.tombstone_env import ADMIN, VARIANT, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-052", "EF-BIB-053", "EF-INT-023")


def test_an_empty_journal_reads_as_absent_not_unreadable(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    path = env.journal_path(env.library)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")  # created, then a crash before the header
    report = env.service().replay(env.roots)
    assert env.library.root_id not in report.unreadable_roots


def test_a_truncated_header_blocks_the_root_until_the_journal_is_set_aside(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(VARIANT, env.roots, reason="purge_unit")
    path = env.journal_path(env.library)
    good = path.read_bytes()
    path.write_bytes(good[:7])  # a header cut short
    report = service.replay(env.roots)
    assert env.library.root_id in report.unreadable_roots
    assert service.check(VARIANT, root_id=env.library.root_id).cause == "journal_unreadable"

    aside = service.set_aside_journal(env.roots, principal=ADMIN, now=NOW)
    assert list(aside) == [env.library.root_id]
    kept = Path(env.library.path) / aside[env.library.root_id]
    assert kept.read_bytes() == good[:7]  # nothing deleted, the damaged file is kept
    assert ".aside-" in kept.name
    # the journal is written again from the database, so the disk carries the tombstone again
    assert service.owed_roots() == []
    rebuilt = path.read_bytes()  # the same header and one line, sealed again (new nonce)
    assert rebuilt.split(b"\n")[0] == good.split(b"\n")[0]
    assert len(rebuilt.splitlines()) == len(good.splitlines())
    after = service.replay(env.roots)
    assert env.library.root_id not in after.unreadable_roots
    assert service.check(VARIANT, root_id=env.library.root_id).blocked  # still tombstoned


def test_setting_a_journal_aside_is_a_reserved_action(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    key = Principal("01J00000000000000000000KEY", "admin", "admin", Credential.API_KEY)
    with pytest.raises(LostKeyringRefusedError):
        service.set_aside_journal(env.roots, principal=key, now=NOW)


def test_lines_owed_are_paid_by_the_next_look_when_the_root_is_back(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(VARIANT, [env.unmounted(env.library), env.books], reason="purge_unit")
    assert service.has_pending(env.library.root_id)
    assert service.owed_roots() == [env.library.root_id]
    # the root is back: flushing it pays the line, and the journal exists
    assert service.flush_pending(env.library)
    assert not service.has_pending(env.library.root_id)
    assert env.journal_path(env.library).exists()


def _owing_instance(inst: Any) -> tuple[Any, str]:
    _, loaded, service = inst.comp.read_context()
    root = next(r for r in loaded.roots if r.mode.value == "rw")
    owed = TombstoneRoot(root.root_id, root.path, mounted=False)
    service.record(VARIANT, [owed], reason="purge_unit")  # a purge while the root was away
    assert service.has_pending(root.root_id)
    return service, root.root_id


def test_a_job_that_opens_a_write_context_pays_the_lines_owed(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        service, root_id = _owing_instance(inst)
        inst.comp.read_context()  # a read pays nothing
        assert service.has_pending(root_id)
        with inst.comp.write_context() as w:  # as `runtime_jobs` does at every run
            assert not service.has_pending(root_id)
            path = next(r.path for r in w.loaded.roots if r.root_id == root_id)
            w.fsops.write_atomic(path + "/probe.txt", io.BytesIO(b"x"))  # no TombstonePending


def test_the_minute_check_pays_the_lines_owed(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        service, root_id = _owing_instance(inst)
        inst.comp.flush_owed_tombstones()  # what `ops.disk_check` calls every minute
        assert not service.has_pending(root_id)


def test_the_waiting_list_names_the_roots_that_owe_lines(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.ops.wiring import open_ops

    with instance(settings, tmp_path) as inst:
        inst.configure()
        _, loaded, service = inst.comp.read_context()
        root = next(r for r in loaded.roots if r.mode.value == "rw")
        service.record(
            VARIANT, [TombstoneRoot(root.root_id, root.path, mounted=False)], reason="purge_unit"
        )
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        owed = [w for w in ops.status().waiting if w.state == "tombstone_lines_owed"]
        assert [w.subject for w in owed] == [root.root_id]
        assert owed[0].command == "shelfhold tombstone flush"
        with inst.engine.begin() as connection:  # a restore is pending: no flush is proposed
            connection.execute(sa.update(platform.instance).values(restore_generation=1))
        frozen = [w for w in ops.status().waiting if w.state == "tombstone_lines_owed"]
        assert frozen[0].command == "shelfhold reconcile --adopt-after-restore"
        with pytest.raises(ExitUnavailableError):
            ops.tombstone_flush()
        with inst.engine.begin() as connection:
            connection.execute(sa.update(platform.instance).values(restore_generation=0))
        assert ops.tombstone_flush() == []
        assert not [w for w in ops.status().waiting if w.state == "tombstone_lines_owed"]


# -- review of PR #69, blocker 2: setting a journal aside must not let purged content come back --


def _backup_then_late_purge(env: object, *, books_away: bool) -> tuple[object, object]:
    """A purge before the backup, one after it that only the journals know, then the restore."""
    service = env.service()  # type: ignore[attr-defined]
    service.replay(env.roots)  # type: ignore[attr-defined]
    late = VariantRef("suwayomi", "9999", "/chapter/other-ch-8", ())
    service.record(VARIANT, env.roots, reason="purge_unit")  # type: ignore[attr-defined]
    backup_seq = max(r["seq"] for r in env.table_rows())  # type: ignore[attr-defined]
    targets = [env.library, env.unmounted(env.books)] if books_away else env.roots  # type: ignore[attr-defined]
    service.record(late, targets, reason="purge_unit")
    with env.engine.begin() as connection:  # type: ignore[attr-defined]  # the database of the backup
        connection.execute(
            sa.delete(coherence.content_tombstone).where(
                coherence.content_tombstone.c.seq > backup_seq
            )
        )
        connection.execute(sa.delete(coherence.tombstone_pending_root))
    return late, service


@pytest.mark.parametrize("books_away", [True, False])
def test_a_purge_only_the_damaged_journal_knew_stays_in_force(
    tmp_path: Path, books_away: bool
) -> None:
    env = make_tomb_env(tmp_path)
    late, _ = _backup_then_late_purge(env, books_away=books_away)
    for root in [env.library] if books_away else env.roots:
        path = env.journal_path(root)
        _, _, rest = path.read_bytes().partition(b"\n")
        path.write_bytes(b"SHELF\n" + rest)  # the header is cut, every line is intact
    service = env.service()
    report = service.replay(env.roots)
    assert env.library.root_id in report.unreadable_roots
    aside = service.set_aside_journal(env.roots, principal=ADMIN, now=NOW)
    assert env.library.root_id in aside
    fresh = env.service()
    fresh.replay(env.roots)
    assert fresh.check(late, root_id=env.library.root_id).blocked  # the purge is not forgotten
    assert fresh.check(late, root_id=None).blocked


def test_setting_aside_is_refused_when_a_line_cannot_be_authenticated(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    service.record(VARIANT, env.roots, reason="purge_unit")
    path = env.journal_path(env.library)
    _, _, rest = path.read_bytes().partition(b"\n")
    path.write_bytes(b"SHELF\n" + rest)
    empty = Keyring.generate(NOW)  # a keyring that lacks the key the lines were sealed with
    blind = env.service(empty)
    blind.replay(env.roots)
    with pytest.raises(SetAsideRefusedError, match="keyring"):
        blind.set_aside_journal(env.roots, principal=ADMIN, now=NOW)
    assert path.exists()  # nothing was moved
