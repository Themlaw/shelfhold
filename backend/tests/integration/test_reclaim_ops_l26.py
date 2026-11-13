"""M3 L26 over a real composition (`l41_env.instance`): the early purge job, the operating side of
the two reserved actions and the size check, with the real keyring, database and fsops."""

import os
import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.composition import Gates
from shelfhold.config import Settings
from shelfhold.export.application.reclaim import TrashEmptyRefusedError
from shelfhold.identity.public import Credential, Principal
from shelfhold.library.public import AddSpaceRefusedError, TrashConfirmationError
from shelfhold.library.tombstones import TombstoneRoot, VariantRef
from shelfhold.library.tombstones.lift import lift_phrase
from shelfhold.ops import reclaim
from shelfhold.retention_jobs import early_purge_trash
from tests.integration.test_retention import make_bin_entry
from tests.l41_env import instance
from tests.purge_env import ADMIN, KEY

pytestmark = pytest.mark.req("EF-BIB-030", "EF-BIB-054")


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
        make_bin_entry(inst.library, "a" * 32, 40)
        make_bin_entry(inst.library, "b" * 32, 10)
        make_bin_entry(inst.library, "c" * 32, 3)  # under the minimum age: never

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
        left = {"calls": 0}

        def pressure(_root: str) -> bool:
            left["calls"] += 1
            return left["calls"] <= 4  # a few measures, then the volume is fine again

        inst.comp.gates = Gates(disk_pressure=pressure)
        for name in ("a", "b", "c", "d", "e", "f"):
            make_bin_entry(inst.library, name * 32, 30)

        early_purge_trash(inst.comp)

        assert 0 < len(_bins(inst.library)) < 6  # it stopped when the pressure was over


def test_trash_empty_over_a_real_composition(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        make_bin_entry(inst.library, "a" * 32, 1)
        make_bin_entry(inst.library, "b" * 32, 40)

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
        entry = make_bin_entry(inst.library, "a" * 32, 40)
        (entry / "link").symlink_to(keep)
        old = time.time() - 40 * 86400
        os.utime(entry, (old, old))

        early_purge_trash(inst.comp)

        assert (keep / "mine.txt").read_text() == "mine" and _bins(inst.library) == []
