"""EF-INT-026, EF-EXP-029, I39 (ADR-0044): the removal of a destination and the full detachment
write through a write session, so they meet the whole-instance guard and the lease.

They used to build their `Fsops` from `Composition.read_context()`: no session, no lease, no look
at the other roots. That `Fsops` is now a view that refuses every write, and `DetachOps` opens a
write session for what deletes. Real `DetachOps`, real instance (only Kavita is simulated).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.remove_destination import RemovalMode, removal_phrase
from shelfhold.infrastructure.fsops.errors import ReadOnlyFsopsError, RootBlockedError
from shelfhold.infrastructure.schema import export, platform
from shelfhold.ops.detach import DetachOps
from tests.integration.test_m4_l34_teardown_jobs import DELETE, _Rig, _target_of, rig  # noqa: F401
from tests.l50_env import seeded
from tests.maintenance_env import principal

# ruff: noqa: F811
pytestmark = pytest.mark.req("EF-INT-026", "EF-EXP-029")

SENTINEL = ".shelfhold-mount-id"


def test_a_removal_that_takes_the_files_is_refused_while_another_root_is_ahead(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        env.export()
        files = env.exported_files()
        assert files
        comp = env.inst.comp
        with env.inst.engine.connect() as connection:
            roots = sorted(
                connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars()
            )
        store = str(settings.store_dir)
        assert store in roots and len(roots) == 2  # the files live on the other root
        sentinel = Path(store) / SENTINEL  # the store comes back ahead of the database
        document = json.loads(sentinel.read_text("utf-8"))
        document["generation"] = int(document["generation"]) + 5
        sentinel.write_text(json.dumps(document), "utf-8")
        ops = DetachOps(comp, SimpleNamespace(), flagged_names=lambda _id: 0)
        phrase = removal_phrase(env.destination_id, RemovalMode.DELETE_FILES)
        with pytest.raises(RootBlockedError):
            ops.remove(
                destination_id=env.destination_id, mode=RemovalMode.DELETE_FILES.value,
                typed=phrase, give_up=False, principal=principal("admin", comp.clock.now()),
            )  # fmt: skip
        assert env.exported_files() == files  # nothing left the disk
        with env.inst.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(export.shelf_destination)
            ).scalar_one()
        assert count == 1  # and the destination was not forgotten


# -- the real read context, and the doors that delete ----------------------------------------


def test_the_read_context_of_the_composition_refuses_every_write(
    settings: Settings, tmp_path: Path
) -> None:
    """`Composition.read_context()` itself (no helper): its `Fsops` writes and deletes nothing."""
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.export()
        files = env.exported_files()
        assert files
        fsops, _loaded, _tombstones = env.inst.comp.read_context()
        with pytest.raises(ReadOnlyFsopsError):
            fsops.write_atomic(str(files[0].parent / "new.bin"), b"x", parents=True)
        with pytest.raises(ReadOnlyFsopsError):
            fsops.unlink(str(files[0]))
        with pytest.raises(ReadOnlyFsopsError):
            fsops.purge_unlink(str(files[0]))
        assert env.exported_files() == files  # and reads still work on the same view
        assert fsops.observe(str(files[0])) is not None


class _Entries:
    """Counts the write sessions `Composition.write_context` opens."""

    def __init__(self) -> None:
        self.count = 0


@pytest.fixture
def entries(monkeypatch: pytest.MonkeyPatch, rig: _Rig) -> _Entries:
    seen = _Entries()
    comp = rig.comp
    real = comp.write_context

    @contextmanager
    def counted() -> Iterator[Any]:
        seen.count += 1
        with real() as context:
            yield context

    monkeypatch.setattr(comp, "write_context", counted)
    return seen


def test_the_removal_that_takes_the_files_opens_a_write_session(
    rig: _Rig, entries: _Entries
) -> None:
    rig.ops.remove(
        destination_id=rig.env.destination_id, mode=DELETE.value, typed=rig.phrase(),
        give_up=False, principal=principal("admin", rig.comp.clock.now()),
    )  # fmt: skip
    assert entries.count >= 1
    assert rig.files() == []  # and it did delete: the session is the door, not a refusal


def test_the_removal_job_opens_a_write_session(rig: _Rig, entries: _Entries) -> None:
    rig.queue_removal()
    entries.count = 0  # the order itself deletes nothing
    rig.run()
    assert entries.count >= 1
    assert rig.files() == []


def test_the_detach_job_opens_a_write_session(rig: _Rig, entries: _Entries) -> None:
    target = _target_of(rig)
    rig.ops.full_queue(
        target_id=target, typed=f"detach {target}", credential=None,
        discard_hidden_data=True, discard_tombstone_journal=True, key_gone=False,
        principal=principal("admin", rig.comp.clock.now()),
    )  # fmt: skip
    entries.count = 0
    rig.run()
    assert entries.count >= 1


def test_the_full_detachment_opens_a_write_session(rig: _Rig, entries: _Entries) -> None:
    target = _target_of(rig)
    with contextlib.suppress(Exception):  # the outcome is not the subject: the door is
        rig.ops.full(
            target_id=target, typed=f"detach {target}", credential=None,
            discard_hidden_data=True, discard_tombstone_journal=True, key_gone=False,
            give_up=False, principal=principal("admin", rig.comp.clock.now()),
        )  # fmt: skip
    assert entries.count >= 1
