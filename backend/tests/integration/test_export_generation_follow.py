"""An export that waits for its scan follows the generation another task legitimately moved.

The `Fsops` of an export is opened with the generation read at that moment. While it waits (the
scan debounce), another write task opens a session: the database commits `generation + 1` and the
sentinel is rewritten. That sentinel is ahead of the copy held by the export, not of the database:
it must not read as `restored_behind`. A real rollback stays refused (EF-EXP-029, EF-INT-026)."""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import RootBlockedError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.l41_env import Instance, instance

SENTINEL = ".shelfhold-mount-id"


def _sentinel(inst: Instance) -> Path:
    return next(
        p / SENTINEL for p in (inst.library, inst.library.parent) if (p / SENTINEL).exists()
    )


def _edit(inst: Instance, **changes: object) -> None:
    path = _sentinel(inst)
    document = json.loads(path.read_text("utf-8"))
    document.update(changes)
    path.write_text(json.dumps(document), "utf-8")


def _db_generation(inst: Instance) -> int:
    with inst.engine.connect() as connection:
        return int(
            connection.execute(
                sa.select(platform.physical_root.c.generation).where(
                    platform.physical_root.c.path_entry == str(inst.library)
                )
            ).scalar_one()
        )


def _another_task_writes(inst: Instance) -> None:
    with inst.comp.write_context():  # a session: the counter moves once, the sentinel follows
        pass


def _opened_export(inst: Instance):  # type: ignore[no-untyped-def]
    """What an export holds: its own `Fsops`, opened at its start, and the root it will scan."""
    fsops, _loaded, _tombstones = inst.comp.read_context()
    fsops.require_scan_allowed(str(inst.library))  # right at the start: the scan would pass
    return fsops


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_export_scan_follows_a_generation_another_task_moved(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)  # the root has a sentinel and a generation
        fsops = _opened_export(inst)
        before = _db_generation(inst)
        _another_task_writes(inst)  # while the export waits for its scan
        assert _db_generation(inst) == before + 1
        fsops.require_scan_allowed(str(inst.library))  # a legitimate advance: the scan goes
        assert fsops.root_state(str(inst.library)) is RootState.OK
        assert not fsops.frozen


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_export_follows_several_advances_then_refuses_a_real_fall_back(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        for _ in range(3):
            _another_task_writes(inst)
        fsops.require_scan_allowed(str(inst.library))
        _edit(inst, generation=_db_generation(inst) - 2)  # then it really falls back
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.STALE


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_a_sentinel_ahead_of_the_database_is_still_restored_behind(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        _edit(inst, generation=_db_generation(inst) + 5)  # the base was restored from a backup
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.RESTORED_BEHIND


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_a_foreign_sentinel_is_never_followed(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        _edit(inst, instance_id="01J00000000000000000000000", generation=_db_generation(inst) + 1)
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.MISMATCH


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_a_generation_that_goes_back_is_never_followed(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        _edit(inst, generation=_db_generation(inst) - 2)  # an old copy of the folder
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.STALE


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_a_restore_epoch_ahead_is_never_followed(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        _another_task_writes(inst)
        _edit(inst, restore_epoch=3)  # a restore the base never heard of
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.RESTORED_BEHIND


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_an_unreachable_database_never_lifts_the_refusal(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        _another_task_writes(inst)

        def broken(_path: str, _root_id: str) -> None:
            raise sa.exc.OperationalError("select", {}, Exception("locked"))

        fsops._generation_source = broken  # the count cannot be read: fail closed
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.RESTORED_BEHIND


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_without_a_generation_source_the_old_verdict_stands(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        _another_task_writes(inst)
        fsops = _opened_export(inst)
        _another_task_writes(inst)
        fsops._generation_source = None
        with pytest.raises(RootBlockedError):
            fsops.require_scan_allowed(str(inst.library))


def _held_after_one_advance(inst: Instance):  # type: ignore[no-untyped-def]
    _another_task_writes(inst)
    fsops = _opened_export(inst)
    _another_task_writes(inst)  # the sentinel is now one ahead of the copy the export holds
    return fsops


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_an_epoch_the_database_moved_on_is_never_followed(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        fsops = _held_after_one_advance(inst)
        with inst.engine.begin() as connection:  # the database now holds another epoch than the
            connection.execute(sa.update(platform.instance).values(restore_epoch=1))  # sentinel
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.RESTORED_BEHIND


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_another_root_registered_at_the_same_path_is_never_followed(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        fsops = _held_after_one_advance(inst)
        with inst.engine.begin() as connection:  # a replaced disk, registered again at the path
            connection.execute(
                sa.update(platform.physical_root)
                .where(platform.physical_root.c.path_entry == str(inst.library))
                .values(root_id=new_ulid())
            )
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.RESTORED_BEHIND


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_a_root_frozen_alone_in_the_database_is_never_followed(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        fsops = _held_after_one_advance(inst)
        with inst.engine.begin() as connection, connection.begin_nested():
            root_id = connection.execute(
                sa.select(platform.physical_root.c.root_id).where(
                    platform.physical_root.c.path_entry == str(inst.library)
                )
            ).scalar_one()
            FreezeState().persist_root(connection, root_id, "root_behind", reason="test")
        with pytest.raises(RootBlockedError):
            fsops.require_scan_allowed(str(inst.library))


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_a_sentinel_the_database_is_ahead_of_is_stale_and_freezes_its_root_only(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        fsops = _held_after_one_advance(inst)
        with inst.engine.begin() as connection:  # the base is two steps past the sentinel
            connection.execute(
                sa.update(platform.physical_root)
                .where(platform.physical_root.c.path_entry == str(inst.library))
                .values(generation=platform.physical_root.c.generation + 2)
            )
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))
        assert caught.value.state is RootState.STALE  # not `restored_behind`
        assert not fsops.frozen  # the instance is not frozen: this root alone is refused
