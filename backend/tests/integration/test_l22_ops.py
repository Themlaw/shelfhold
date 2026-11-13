"""M3 L22 on a real instance: the ops behind the doors. A plan changes nothing; a merge acts for the
fingerprint it was shown; « Séparer » undoes it within 30 days; the actions are reserved (never a
key, never a stale session); each audit line names no title and is written with the rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.ops.errors import CodedRefusal, StalePreviewError, UnknownSubjectError
from shelfhold.ops.work_merge import WorkMergeOps
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-BIB-019", "EF-EXP-062", "EF-BIB-025", "EF-USR-024")

ADMIN = Principal("1000", "uid:1000", "admin", Credential.LOCAL_CLI)
KEY = Principal("u", "admin", "admin", Credential.API_KEY, key_id="k")
STALE = Principal("u", "admin", "admin", Credential.SESSION, session_id="s")


def _works(env, inst) -> tuple[str, str, str, str]:  # type: ignore[no-untyped-def]
    keep, drop = env.new_series("Un roman"), env.new_series("Un roman (bis)")
    with inst.engine.begin() as c:
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == drop.edition_id)
            .values(publisher="Beta")
        )
    return keep.work_id, drop.work_id, keep.edition_id, drop.edition_id


def _work_ids(inst) -> set[str]:  # type: ignore[no-untyped-def]
    with inst.engine.connect() as c:
        return set(c.execute(sa.select(catalog.work.c.id)).scalars())


def test_work_merge_plan_apply_separate_and_audit(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        keep, drop, _ke, de = _works(env, inst)
        before = _work_ids(inst)
        plan = ops.merge(keep, drop, confirm=None, principal=ADMIN)
        assert plan["mergeable"] and plan["applied"] is False and plan["merge_id"] is None
        assert _work_ids(inst) == before  # a plan writes nothing
        with pytest.raises(StalePreviewError):  # an admin, but not for this plan
            ops.merge(keep, drop, confirm="0" * 16, principal=ADMIN)
        done = ops.merge(keep, drop, confirm=plan["fingerprint"], principal=ADMIN)
        assert done["applied"] is True and drop not in _work_ids(inst)
        back = ops.separate(done["merge_id"], principal=ADMIN)
        assert back["restored_id"] == drop and drop in _work_ids(inst)
        with inst.engine.connect() as c:
            owner = c.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == de)
            ).scalar_one()
        assert owner == drop
        with pytest.raises(CodedRefusal) as twice:
            ops.separate(done["merge_id"], principal=ADMIN)
        assert twice.value.code == "merge.already_undone"
        rows = list(env.rows(identity.audit))
        actions = [r.action for r in rows if r.action.startswith("work.")]
        assert actions == ["work.merge", "work.separate"]
        assert all("Un roman" not in str(r.target) for r in rows)


def test_work_merge_refusals_and_unknown_subjects(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        keep, drop, _ke, _de = _works(env, inst)
        with pytest.raises(UnknownSubjectError):
            ops.merge(keep, new_ulid(), confirm=None, principal=ADMIN)
        with pytest.raises(CodedRefusal) as same:
            ops.merge(keep, keep, confirm=None, principal=ADMIN)
        assert same.value.code == "work.merge_refused"
        with inst.engine.begin() as c:  # two Editions that would share a key
            c.execute(sa.update(catalog.edition).values(publisher=""))
        refused = ops.merge(keep, drop, confirm=None, principal=ADMIN)
        assert refused["blockers"] == ["collision:edition_key"] and not refused["mergeable"]
        with pytest.raises(CodedRefusal) as blocked:
            ops.merge(keep, drop, confirm=refused["fingerprint"], principal=ADMIN)
        assert blocked.value.code == "work.merge_refused"
        assert {keep, drop} <= _work_ids(inst)
        with pytest.raises(UnknownSubjectError):
            ops.separate(new_ulid(), principal=ADMIN)


def test_separate_after_30_days_is_refused(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        keep, drop, _ke, _de = _works(env, inst)
        plan = ops.merge(keep, drop, confirm=None, principal=ADMIN)
        done = ops.merge(keep, drop, confirm=plan["fingerprint"], principal=ADMIN)
        old = datetime.now(UTC) - timedelta(days=31)
        with inst.engine.begin() as c:
            c.execute(sa.update(catalog.merge_log).values(created_at=old))
        with pytest.raises(CodedRefusal) as late:
            ops.separate(done["merge_id"], principal=ADMIN)
        assert late.value.code == "merge.expired"
        assert drop not in _work_ids(inst)  # the merge stays


def test_the_actions_are_reserved_never_a_key_or_a_stale_session(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        keep, drop, _ke, _de = _works(env, inst)
        before = _work_ids(inst)
        for who in (KEY, STALE):
            with pytest.raises(CodedRefusal):
                ops.merge(keep, drop, confirm="0" * 16, principal=who)
            with pytest.raises(CodedRefusal):
                ops.separate(new_ulid(), principal=who)
            with pytest.raises(CodedRefusal):
                ops.series_reassign(keep, confirm=None, principal=who)
        assert _work_ids(inst) == before


def test_series_plan_and_late_attach(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        keep, drop, _ke, _de = _works(env, inst)
        sid = new_ulid()
        with inst.engine.begin() as c:
            c.execute(sa.insert(catalog.series).values(id=sid, title="S", sort_title="s"))
        shown = ops.attach_series(keep, sid, "1", principal=ADMIN)
        assert shown["reassignment"]["applied"] is False  # the plan is shown, never applied
        with pytest.raises(CodedRefusal) as twice:
            ops.attach_series(keep, sid, "2", principal=ADMIN)
        assert twice.value.code == "series.already_attached"
        with pytest.raises(UnknownSubjectError):
            ops.attach_series(drop, new_ulid(), "1", principal=ADMIN)
        plan = ops.series_reassign(keep, confirm=None, principal=ADMIN)
        assert plan["moves"] == [] and plan["applied"] is False
        with pytest.raises(StalePreviewError):
            ops.series_reassign(keep, confirm="0" * 16, principal=ADMIN)
        done = ops.series_reassign(keep, confirm=plan["fingerprint"], principal=ADMIN)
        assert done["applied"] is True and done["moves"] == []
        actions = {r.action for r in env.rows(identity.audit)}
        assert {"series.attach", "series.reassign"} <= actions
