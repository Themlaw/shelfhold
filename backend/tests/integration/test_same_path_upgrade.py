"""EF-EXP-016: the upgrade of an exported file at the same path, under the intent journal.

The old version goes to the bin of its root and can be put back; every automatic upgrade says
that KOReader's position was reset; a read Unit is left alone by default, whatever the path that
asks; an irreplaceable Unit needs its identifier typed; a frozen root is never written.
"""

import errno
import os
import time
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.recycle_sweep import RecycleSweeper
from shelfhold.export.application.upgrade import UPGRADE_REASONS, UpgradeOutcome
from shelfhold.export.domain.physical_roots import RECYCLE_DIR, SENTINEL_FILE
from shelfhold.infrastructure.fsops import confined
from shelfhold.infrastructure.schema import coherence, export, platform, storage
from shelfhold.ops.status import koreader_reset_report
from tests.export_env import export_env
from tests.upgrade_env import (
    better_copy,
    bin_files,
    digest,
    export_one,
    make_irreplaceable,
    mark_read,
    record_of,
    request,
    service,
)

pytestmark = pytest.mark.req("EF-EXP-016")


def _events(env: object, event_type: str) -> list[sa.Row[tuple[object, ...]]]:
    with env.engine.connect() as connection:  # type: ignore[attr-defined]
        return list(
            connection.execute(sa.select(platform.event).where(platform.event.c.type == event_type))
        )


def test_same_path_upgrade_moves_old_to_recycle() -> None:
    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        scans: list[str] = []

        result = service(env, scans=scans).upgrade(request(env, exported, content_id))

        assert result.outcome is UpgradeOutcome.UPGRADED
        assert env.files() == [exported.target]  # the same path, one file served
        assert exported.target.read_bytes() != old_bytes
        (binned,) = bin_files(env)
        assert binned.read_bytes() == old_bytes  # the old version, whole, in the root's bin
        assert str(binned).endswith(str(exported.target.relative_to(env.lib.library)))
        row = record_of(env, exported)
        assert row.file_hash == digest(exported.target)  # the manifest says what the disk says
        assert row.rel_path is not None and str(exported.target).endswith(str(row.rel_path))
        assert row.state == "written"
        with env.engine.connect() as connection:
            item = connection.execute(
                sa.select(export.export_item).where(
                    export.export_item.c.id == exported.export_item_id
                )
            ).one()
            trash = connection.execute(sa.select(storage.trash_item)).one()
            aliases = connection.execute(
                sa.select(export.document_alias.c.file_hash).where(
                    export.document_alias.c.export_item_id == exported.export_item_id
                )
            ).scalars()
            assert open_intent_paths(connection) == frozenset()
            done = connection.execute(
                sa.select(coherence.fs_intent.c.state).where(coherence.fs_intent.c.op == "upgrade")
            ).scalar_one()
        assert item.content_id == content_id  # the same export item: Kavita keeps its progress
        assert trash.kind == "export_file" and trash.ref == exported.export_item_id
        assert exported.old_file_hash in set(aliases)  # a position on the old file still resolves
        assert done == "done"
        assert scans  # Kavita is asked to look at the folder again


def test_auto_upgrade_logs_koreader_reset() -> None:
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)

        service(env).upgrade(request(env, exported, content_id, reason="preferred_team"))

        (event,) = _events(env, "unit.upgraded")
        assert event.payload["koreader_reset"] is True
        assert event.payload["automatic"] is True
        assert event.payload["reason"] == "preferred_team"
        assert event.payload["unit_id"] == exported.unit_id
        assert event.payload["export_item_id"] == exported.export_item_id
        with env.engine.connect() as connection:
            listed = koreader_reset_report(connection, env.clock.now() - timedelta(days=1))
        assert [entry["unit_id"] for entry in listed] == [exported.unit_id]  # `status` (D21)


@pytest.mark.parametrize("reason", sorted(UPGRADE_REASONS))
def test_skip_upgrade_of_read_units_by_default(reason: str) -> None:
    with export_env() as env:
        exported = export_one(env)
        before = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        mark_read(env, exported)

        result = service(env).upgrade(request(env, exported, content_id, reason=reason))

        assert result.outcome is UpgradeOutcome.SKIPPED_READ
        assert exported.target.read_bytes() == before
        assert bin_files(env) == []
        with env.engine.connect() as connection:
            intents = connection.execute(
                sa.select(sa.func.count())
                .select_from(coherence.fs_intent)
                .where(coherence.fs_intent.c.op == "upgrade")
            ).scalar_one()
        assert intents == 0  # nothing was even announced


def test_read_unit_is_upgraded_when_the_shelf_allows_it() -> None:
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)
        mark_read(env, exported)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(skip_read_upgrades=False))

        result = service(env).upgrade(request(env, exported, content_id, reason="better_version"))

        assert result.outcome is UpgradeOutcome.UPGRADED


@pytest.mark.req("EF-BIB-031")
def test_irreplaceable_unit_is_never_upgraded_without_typed_confirmation() -> None:
    with export_env() as env:
        exported = export_one(env)
        before = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        make_irreplaceable(env)
        upgrade = service(env)

        refused = upgrade.upgrade(request(env, exported, content_id))
        wrong = upgrade.upgrade(request(env, exported, content_id, automatic=False, typed="x"))

        assert refused.outcome is UpgradeOutcome.NEEDS_CONFIRMATION
        assert wrong.outcome is UpgradeOutcome.NEEDS_CONFIRMATION
        assert exported.target.read_bytes() == before and bin_files(env) == []

        typed = upgrade.upgrade(
            request(env, exported, content_id, automatic=False, typed=exported.unit_id)
        )
        assert typed.outcome is UpgradeOutcome.UPGRADED


@pytest.mark.req("EF-EXP-064")
def test_upgrade_never_writes_on_a_frozen_root() -> None:
    with export_env() as env:
        exported = export_one(env)
        before = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        env.lib.fsops.freeze_state.follow_root(env.lib.library_root, "foreign_root")

        result = service(env).upgrade(request(env, exported, content_id))

        assert result.outcome is UpgradeOutcome.ROOT_FROZEN
        assert exported.target.read_bytes() == before
        assert bin_files(env) == []
        for host in ("staging", "upgrade"):
            assert not any(p.is_file() for p in (env.lib.library / ".shelfhold" / host).rglob("*"))
        with env.engine.connect() as connection:
            assert connection.execute(
                sa.select(sa.func.count()).select_from(coherence.fs_intent)
                .where(coherence.fs_intent.c.op == "upgrade")
            ).scalar_one() == 0  # fmt: skip


@pytest.mark.req("EF-EXP-029")
def test_upgrade_on_a_root_blocked_by_its_sentinel_records_nothing() -> None:
    """A root whose sentinel does not match is as untouchable as a frozen one: the refusal comes
    before the journal, so not even a cancelled intent is left behind."""
    with export_env() as env:
        exported = export_one(env)
        before = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        (env.lib.library / SENTINEL_FILE).write_text("garbage\n")

        result = service(env).upgrade(request(env, exported, content_id))

        assert result.outcome is UpgradeOutcome.ROOT_FROZEN
        assert exported.target.read_bytes() == before
        assert bin_files(env) == []
        with env.engine.connect() as connection:
            assert connection.execute(
                sa.select(sa.func.count()).select_from(coherence.fs_intent)
                .where(coherence.fs_intent.c.op == "upgrade")
            ).scalar_one() == 0  # fmt: skip


def test_up_to_date_unit_is_left_alone() -> None:
    with export_env() as env:
        exported = export_one(env)
        with env.engine.connect() as connection:
            current = connection.execute(
                sa.select(export.export_item.c.content_id).where(
                    export.export_item.c.id == exported.export_item_id
                )
            ).scalar_one()

        result = service(env).upgrade(request(env, exported, str(current)))

        assert result.outcome is UpgradeOutcome.UP_TO_DATE
        assert bin_files(env) == []


def test_upgrade_restore_puts_the_old_version_back() -> None:
    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        upgrade = service(env)
        done = upgrade.upgrade(request(env, exported, content_id))
        new_bytes = exported.target.read_bytes()
        assert done.upgrade_id is not None

        back = upgrade.restore(done.upgrade_id)

        assert back.outcome is UpgradeOutcome.UPGRADED
        assert env.files() == [exported.target]
        assert exported.target.read_bytes() == old_bytes
        assert new_bytes in {p.read_bytes() for p in bin_files(env)}  # the newer one is binned
        row = record_of(env, exported)
        assert row.file_hash == exported.old_file_hash
        assert row.content_hash == exported.old_content_hash
        with env.engine.connect() as connection:
            states = dict(
                connection.execute(
                    sa.select(export.export_upgrade.c.id, export.export_upgrade.c.state)
                ).all()
            )
        assert states[done.upgrade_id] == "restored"


def test_restore_reads_the_bin_path_recorded_not_the_current_one() -> None:
    """The served file was renamed after the upgrade: the restore still finds the old version at
    the path the bin index recorded, and puts it back at the current path."""
    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        upgrade = service(env)
        done = upgrade.upgrade(request(env, exported, content_id))
        assert done.upgrade_id is not None
        record = export.export_record
        row = record_of(env, exported)
        renamed_rel = str(row.rel_path).replace(".cbz", " (renamed).cbz")
        assert renamed_rel != row.rel_path
        renamed = exported.target.with_name(renamed_rel.rsplit("/", 1)[-1])
        exported.target.rename(renamed)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(record)
                .where(record.c.shelf_destination_id == env.destination_id,
                       record.c.export_item_id == exported.export_item_id)
                .values(rel_path=renamed_rel)
            )  # fmt: skip

        back = upgrade.restore(done.upgrade_id)

        assert back.outcome is UpgradeOutcome.UPGRADED
        assert env.files() == [renamed]
        assert renamed.read_bytes() == old_bytes


@pytest.mark.req("ENF-OPS-009")
def test_retention_sweep_forgets_the_bin_rows_it_removes() -> None:
    """A swept upgrade bin entry leaves the bin index: no restore points at a removed file."""
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)
        done = service(env).upgrade(request(env, exported, content_id))
        assert done.upgrade_id is not None
        (entry,) = (env.lib.library / RECYCLE_DIR).iterdir()
        old = time.time() - 31 * 86400
        os.utime(entry, (old, old))
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)

        report = RecycleSweeper(env.lib.fsops, journal, [str(env.lib.library)]).sweep()

        assert report.removed == 1
        assert bin_files(env) == []
        with env.engine.connect() as connection:
            assert connection.execute(
                sa.select(sa.func.count()).select_from(storage.trash_item)
                .where(storage.trash_item.c.kind == "export_file")
            ).scalar_one() == 0  # fmt: skip
            trash_item_id = connection.execute(
                sa.select(export.export_upgrade.c.trash_item_id)
                .where(export.export_upgrade.c.id == done.upgrade_id)
            ).scalar_one()  # fmt: skip
        assert trash_item_id is None
        with pytest.raises(LookupError):
            service(env).restore(done.upgrade_id)


def test_exchange_unsupported_still_serves_one_file(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_exchange(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EINVAL, "no RENAME_EXCHANGE here")

    monkeypatch.setattr(confined, "renameat2_exchange", no_exchange)
    with export_env() as env:
        exported = export_one(env)
        old_bytes = exported.target.read_bytes()
        content_id = better_copy(env, exported)

        result = service(env).upgrade(request(env, exported, content_id))

        assert result.outcome is UpgradeOutcome.UPGRADED
        assert env.files() == [exported.target]
        assert [p.read_bytes() for p in bin_files(env)] == [old_bytes]
        assert record_of(env, exported).file_hash == digest(exported.target)


@pytest.mark.req("EF-BIB-043")
def test_pre_m2_revision_is_never_an_upgrade_reason() -> None:
    """A revision recorded before M2 is never applied by an upgrade: it is not one of its
    reasons, so no path can hand it in (EF-BIB-043 is the queue of L17)."""
    assert "revision" not in UPGRADE_REASONS
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)
        with pytest.raises(ValueError, match="reason"):
            service(env).upgrade(request(env, exported, content_id, reason="revision"))


def test_upgrade_intent_is_settled_by_the_boot_replay() -> None:
    """The hook the boot passes to `resolve_open` completes an upgrade the journal finishes."""
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)
        upgrade = service(env)
        upgrade.upgrade(request(env, exported, content_id))
        journal = IntentJournal(env.engine, env.lib.fsops, clock=env.clock)

        report = journal.resolve_open(on_done=upgrade.settle_hook())

        assert [*report.refused, *report.drifted] == []


def test_shelf_option_defaults_to_skipping_read_units() -> None:
    with export_env() as env, env.engine.connect() as connection:
        flags = connection.execute(sa.select(export.shelf.c.skip_read_upgrades)).scalars()
        assert all(flags)
