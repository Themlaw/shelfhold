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

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.library.tombstones.service import LostKeyringRefusedError, TombstoneRoot
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


def test_a_job_that_opens_its_context_pays_the_lines_owed(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        service, root_id = _owing_instance(inst)
        fsops, loaded, _ = inst.comp.read_context()  # as `runtime_jobs` does at every run
        assert not service.has_pending(root_id)
        path = next(r.path for r in loaded.roots if r.root_id == root_id)
        fsops.write_atomic(path + "/probe.txt", io.BytesIO(b"x"))  # no TombstonePendingError


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
        assert ops.tombstone_flush() == []
        assert not [w for w in ops.status().waiting if w.state == "tombstone_lines_owed"]
