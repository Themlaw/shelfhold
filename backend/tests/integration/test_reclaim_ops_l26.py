"""M3 L26 over a real composition (`l41_env.instance`): the early purge job, the operating side of
the two reserved actions and the size check, with the real keyring, database and fsops."""

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.composition import Gates
from shelfhold.config import Settings
from shelfhold.export.application.reclaim import TrashEmptyRefusedError
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import AddSpaceRefusedError, TrashConfirmationError
from shelfhold.library.tombstones import TombstoneRoot, VariantRef
from shelfhold.library.tombstones.lift import lift_phrase
from shelfhold.ops import reclaim
from shelfhold.retention_jobs import early_purge_trash
from tests.integration.test_retention import make_bin_entry
from tests.l41_env import instance
from tests.purge_env import ADMIN, KEY

pytestmark = pytest.mark.req("EF-BIB-030", "EF-BIB-054")


def known_entry(inst: Any, name: str, age_days: int) -> Path:
    """A bin entry the index knows (a deletion wrote its row and its term), as a real one is."""
    entry = make_bin_entry(inst.library, name, age_days)
    now = datetime.now(UTC)
    with inst.engine.begin() as connection:
        connection.execute(
            sa.insert(storage.trash_item).values(
                id=new_ulid(),
                kind="work",
                ref=new_ulid(),
                path=f"{name}/chapter.cbz",
                deleted_at=now - timedelta(days=age_days),
                purge_after=now + timedelta(days=30 - age_days),
                snapshot={"place": "library"},
                level="delete_content",
            )
        )
    return entry


def _bins(library: Path) -> list[str]:
    return sorted(p.name for p in (library / ".shelfhold" / "recycle").glob("*"))


def test_the_early_purge_job_does_nothing_without_a_disk_guard(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        make_bin_entry(inst.library, "a" * 32, 40)
        assert early_purge_trash(inst.comp).removed == 0  # a process with no guard: no pressure
        assert _bins(inst.library) == ["a" * 32]


def test_the_early_purge_job_takes_the_old_entries_under_pressure_and_only_those(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        inst.comp.gates = Gates(disk_pressure=lambda _root: True)
        known_entry(inst, "a" * 32, 40)
        known_entry(inst, "b" * 32, 10)
        known_entry(inst, "c" * 32, 3)  # under the minimum age: never

        report = early_purge_trash(inst.comp)

        assert report.removed == 2 and _bins(inst.library) == ["c" * 32]
        with inst.engine.connect() as conn:
            [(actor, target)] = conn.execute(
                sa.text("SELECT actor_id, target FROM audit WHERE action = 'trash.early_purged'")
            ).all()
        assert (actor, target) == ("system", "entries:2")


def test_the_early_purge_job_ends_with_the_pressure(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()

        def pressure(_root: str) -> bool:
            return len(_bins(inst.library)) > 3  # fine again once three entries are left

        inst.comp.gates = Gates(disk_pressure=pressure)
        for name in ("a", "b", "c", "d", "e", "f"):
            known_entry(inst, name * 32, 30)

        early_purge_trash(inst.comp)

        assert len(_bins(inst.library)) == 3  # it stopped when the pressure was over


def test_trash_empty_over_a_real_composition(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        known_entry(inst, "a" * 32, 1)
        known_entry(inst, "b" * 32, 40)

        with pytest.raises(TrashConfirmationError):
            reclaim.trash_empty(inst.comp, typed="nope", principal=ADMIN)
        with pytest.raises(TrashEmptyRefusedError):
            reclaim.trash_empty(inst.comp, typed="empty-trash", principal=KEY)
        assert len(_bins(inst.library)) == 2

        done = reclaim.trash_empty(inst.comp, typed="empty-trash", principal=ADMIN)

        assert done["removed"] == 2 and _bins(inst.library) == []


def test_space_check_over_a_real_composition(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        assert reclaim.space_check(inst.comp, estimated_bytes=0)["fits"] is True
        with pytest.raises(AddSpaceRefusedError) as refused:
            reclaim.space_check(inst.comp, estimated_bytes=2**62)
        assert refused.value.estimated_bytes == 2**62 and refused.value.free_bytes > 0


def test_lift_over_a_real_composition_frees_the_candidate_and_is_audited(
    settings: Settings, tmp_path: Path
) -> None:
    candidate = VariantRef("source-a", "1", "/chapter/some-title-ch-7")
    with instance(settings, tmp_path) as inst:
        inst.configure()
        with inst.comp.write_context() as w:
            roots = [TombstoneRoot(r.root_id, r.path, True, r.predecessors) for r in w.loaded.roots]
            seq = w.tombstones.record(candidate, roots).seq
            assert w.tombstones.check(candidate, root_id=None).blocked
        member = Principal(ADMIN.user_id, "member", "member", Credential.LOCAL_CLI)
        with pytest.raises(Exception, match="refused"):
            reclaim.lift_tombstone(inst.comp, seq=seq, typed=lift_phrase(seq), principal=member)

        done = reclaim.lift_tombstone(inst.comp, seq=seq, typed=lift_phrase(seq), principal=ADMIN)

        assert done["seq"] == seq
        with inst.comp.write_context() as w:
            assert not w.tombstones.check(candidate, root_id=None).blocked
        with inst.engine.connect() as conn:
            [(target,)] = conn.execute(
                sa.text("SELECT target FROM audit WHERE action = 'tombstone.lifted'")
            ).all()
        assert target == f"seq:{seq}" and "some-title" not in target


def test_the_early_purge_never_follows_a_link_out_of_the_bin(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        inst.comp.gates = Gates(disk_pressure=lambda _root: True)
        keep = tmp_path / "elsewhere"
        keep.mkdir()
        (keep / "mine.txt").write_text("mine")
        entry = known_entry(inst, "a" * 32, 40)
        (entry / "link").symlink_to(keep)
        old = time.time() - 40 * 86400
        os.utime(entry, (old, old))

        early_purge_trash(inst.comp)

        assert (keep / "mine.txt").read_text() == "mine" and _bins(inst.library) == []


def test_an_entry_the_index_does_not_know_is_kept_by_the_emptying_and_the_early_purge(
    settings: Settings, tmp_path: Path
) -> None:
    """A database restored from before the deletion (or an old version of an upgrade) leaves a bin
    entry with no row: nothing says it is a copy, so neither the manual emptying nor the early
    purge takes it, whatever its age. It is counted, and said (the 30-day sweep decides)."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        inst.comp.gates = Gates(disk_pressure=lambda _root: True)
        stray = make_bin_entry(inst.library, "a" * 32, 40)  # no trash_item row
        known = known_entry(inst, "b" * 32, 40)

        early = early_purge_trash(inst.comp)
        assert early.removed == 1 and stray.exists() and not known.exists()

        done = reclaim.trash_empty(inst.comp, typed="empty-trash", principal=ADMIN)
        assert done["removed"] == 0 and done["held_other"] == 1 and stray.exists()
