"""M3 L22, adversarial review of #219: proofs of concept. Each test is red on the head of
the PR and turns green when its objection is lifted (docs/decisions/M3.md, « L22 : contradicteur
de la #219 »). Real database, real schema, the admin's real rating door; no Edition here
has an age of its own, so each one counts for its Work's (the existing tests rate the Editions
too, which hides what a Work's age carries)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    apply_work_merge,
    plan_work_merge,
    separate_works,
    visible_edition_ids,
)
from shelfhold.config import Settings
from shelfhold.export.application.export_items import (
    apply_series_reassignment,
    ensure_export_series,
    plan_series_reassignment,
)
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.identity.domain.visibility import ViewerScope
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity
from shelfhold.ops.work_merge import WorkMergeOps
from tests.export_env import ExportEnv, Series
from tests.family_env import NOW, family_env, rate
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-BIB-019", "EF-BIB-037", "EF-BIB-055", "EF-EXP-062")

CHILD = ViewerScope("ceiling", 13)  # the child of `family_env`


@pytest.fixture
def fam() -> Iterator[ExportEnv]:
    with family_env() as e:
        yield e


def _work(env: ExportEnv, title: str, age: int, publisher: str) -> Series:
    """A Work rated by the admin's door: its Edition has no age of its own."""
    series = env.new_series(title)
    with env.engine.begin() as c:  # Editions that never share a key
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == series.edition_id)
            .values(publisher=publisher)
        )
        own = c.execute(
            sa.select(catalog.edition.c.age_rating, catalog.edition.c.age_x).where(
                catalog.edition.c.id == series.edition_id
            )
        ).one()
    assert own.age_rating is None and not own.age_x  # it counts for its Work's age
    rate(env, series, age)
    return series


def _merge(env: ExportEnv, keep: str, drop: str, *, at=NOW):  # type: ignore[no-untyped-def]
    with env.engine.connect() as c:
        plan = plan_work_merge(c, keep, drop)
    with env.engine.begin() as c:
        return apply_work_merge(c, keep, drop, confirm=plan.fingerprint, now=at)


def _child_sees(env: ExportEnv, edition_id: str) -> bool:
    with env.engine.connect() as c:
        return edition_id in visible_edition_ids(c, CHILD, [edition_id])


def _suggest_work(env: ExportEnv, work_id: str, value: int, state: str) -> None:
    with env.engine.begin() as c:
        c.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type="work", entity_id=work_id, value=value, x=False,
                provenance="provider", seen_at=NOW, state=state,
            )
        )  # fmt: skip


def test_review_poc_merge_deletes_the_open_suggestion_that_hid_the_absorbed_work(
    fam: ExportEnv,
) -> None:
    """B1. `_move_entity_rows` drops a row of the absorbed Work that collides with one the survivor
    holds. For `age_suggestion` the unique key is (entity, value, x, provenance), **not** the state:
    the survivor's rejected 16 erases the absorbed Work's open 16, the one barrier that hid it."""
    env = fam
    keep = _work(env, "Alpha Tale", 10, "Alpha")
    drop = _work(env, "Alpha Tale (copy)", 10, "Beta")
    _suggest_work(env, keep.work_id, 16, "rejected")  # the admin looked at this one: it is fine
    _suggest_work(env, drop.work_id, 16, "open")  # nobody looked at this one yet: "À classer"
    assert _child_sees(env, keep.edition_id)
    assert not _child_sees(env, drop.edition_id)  # hidden by its open suggestion (EF-BIB-055)
    _merge(env, keep.work_id, drop.work_id)
    assert not _child_sees(env, drop.edition_id), "a merge showed the child an Edition it hid"


def test_review_poc_separate_gives_the_absorbed_work_back_an_age_lower_than_the_admin_set(
    fam: ExportEnv,
) -> None:
    """B2. The admin raises the merged Work to 16 (by the real door: the Work's age,
    not the Editions').
    « Séparer » keeps 16 on the survivor, but puts the absorbed Work back at its old 10 from the
    snapshot: its Edition, 16+ the minute before, is 10+ again and shown to the child."""
    env = fam
    keep = _work(env, "Alpha Tale", 10, "Alpha")
    drop = _work(env, "Alpha Tale (copy)", 10, "Beta")
    merged = _merge(env, keep.work_id, drop.work_id)
    rate(env, keep, 16)  # the admin read the merged Work and rated it 16
    assert not _child_sees(env, drop.edition_id)
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW + timedelta(days=1))
    assert not _child_sees(env, drop.edition_id), "an undo lowered an age the admin raised"


def test_review_poc_separating_an_older_merge_lowers_the_editions_of_a_later_one(
    fam: ExportEnv,
) -> None:
    """B3. Merge 1 takes D (16) into K (0): K is 16. Merge 2 takes E (16) into K: K's row does not
    change, so it is still the `after` of merge 1. Separating merge 1 then restores K's 0 while K
    holds E's Edition: an Edition of a 16+ Work shown to the child, by an undo."""
    env = fam
    k = _work(env, "Alpha Tale", 0, "Alpha")
    d = _work(env, "Alpha Tale (copy)", 16, "Beta")
    e = _work(env, "Alpha Tale (third)", 16, "Gamma")
    first = _merge(env, k.work_id, d.work_id)
    _merge(env, k.work_id, e.work_id, at=NOW + timedelta(hours=1))
    assert not _child_sees(env, e.edition_id)
    with env.engine.begin() as c:
        separate_works(c, first.merge_id, now=NOW + timedelta(days=1))
    assert not _child_sees(env, e.edition_id), "separating merge 1 lowered merge 2's Edition"


def _library_series(env: ExportEnv, edition_id: str, name: str) -> str:
    with env.engine.begin() as c:
        return ensure_export_series(
            c, shelf_destination_id=env.destination_id, remote_library_id=env.remote_library_id,
            ref=SeriesRef(edition_id, name, name.lower()), now=NOW,
        )  # fmt: skip


def test_review_poc_separate_leaves_the_restored_work_in_the_survivors_exported_series(
    fam: ExportEnv,
) -> None:
    """M1. EF-EXP-062: reassignment « à la fusion et à la séparation ». After a merge and its
    series plan, « Séparer » gives nothing: the restored Work stays exported inside the survivor's
    series, and the plan of the restored Work sees a single series, so it never proposes a move."""
    env = fam
    keep = _work(env, "Alpha Tale", 10, "Alpha")
    drop = _work(env, "Alpha Tale (copy)", 10, "Beta")
    a = _library_series(env, keep.edition_id, "Alpha Tale")
    _library_series(env, drop.edition_id, "Alpha Tale (copy)")
    merged = _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        plan = plan_series_reassignment(c, keep.work_id)
    with env.engine.begin() as c:
        apply_series_reassignment(c, keep.work_id, confirm=plan.fingerprint, now=NOW)
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW + timedelta(days=1))
    link = export.edition_export_series
    with env.engine.connect() as c:
        linked = c.execute(
            sa.select(link.c.export_series_id).where(link.c.edition_id == drop.edition_id)
        ).scalar_one()
        back = plan_series_reassignment(c, drop.work_id)
    assert linked != a or back.moves, "the separated Work stays in the survivor's series, no plan"


def test_review_poc_a_session_merge_and_separation_are_audited_with_their_target(
    settings: Settings, tmp_path: Path
) -> None:
    """M2. Over HTTP the guard writes `work.merge` / `work.separate` **without a target**
    (`IdentityService.authorize` → `_audit(action.name, principal)`), before the plan is even read,
    and `WorkMergeOps._audit` returns for a session. The line that names the Works, the fingerprint
    and the merge is written only for the host CLI. Here: the ops of a fresh admin session."""
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        session = Principal(
            "u", "admin", "admin", Credential.SESSION, session_id=new_ulid(),
            auth_time=inst.comp.clock.now(),
        )  # fmt: skip
        keep, drop = env.new_series("Un roman"), env.new_series("Un roman (bis)")
        with inst.engine.begin() as c:
            c.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == drop.edition_id)
                .values(publisher="Beta")
            )
        plan = ops.merge(keep.work_id, drop.work_id, confirm=None, principal=session)
        done = ops.merge(keep.work_id, drop.work_id, confirm=plan["fingerprint"], principal=session)
        ops.separate(done["merge_id"], principal=session)
        rows = [r for r in env.rows(identity.audit) if r.action.startswith("work.")]
        targets = {r.action: str(r.target) for r in rows}
        assert done["merge_id"] in targets.get("work.merge", ""), "no merge line names its target"
        assert done["merge_id"] in targets.get("work.separate", ""), "nor the separation's"
