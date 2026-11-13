"""M3 L22, counter-review of the fixes of #219 (Opus): attacks through the admin's real rating door
(`RatingService.rate`, the Work's age only; no Edition here has an age of its own unless said).
docs/decisions/M3.md, « L22 : contre-relecture »."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
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
from shelfhold.identity.domain.visibility import ViewerScope
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity
from shelfhold.ops.work_merge import WorkMergeOps
from shelfhold.sdk.age import AgeRating
from tests.export_env import ExportEnv, Series
from tests.family_env import NOW, RATING, admin, family_env
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-BIB-019", "EF-BIB-037", "EF-BIB-055")

CHILD = ViewerScope("ceiling", 13)
H = timedelta(hours=1)


@pytest.fixture
def fam() -> Iterator[ExportEnv]:
    with family_env() as e:
        yield e


def _rate(env: ExportEnv, work_id: str, age: int, at: datetime) -> None:
    with env.engine.begin() as c:
        RATING.rate(c, work_id, AgeRating(age), principal=replace(admin(), auth_time=at), now=at)


def _work(env: ExportEnv, title: str, age: int, publisher: str) -> Series:
    series = env.new_series(title)
    with env.engine.begin() as c:
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == series.edition_id)
            .values(publisher=publisher)
        )
    _rate(env, series.work_id, age, NOW - 10 * H)
    return series


def _merge(env: ExportEnv, keep: str, drop: str, at: datetime) -> str:
    with env.engine.connect() as c:
        plan = plan_work_merge(c, keep, drop)
    assert plan.mergeable, plan.blockers
    with env.engine.begin() as c:
        return apply_work_merge(c, keep, drop, confirm=plan.fingerprint, now=at).merge_id


def _separate(env: ExportEnv, merge_id: str, at: datetime) -> None:
    with env.engine.begin() as c:
        separate_works(c, merge_id, now=at)


def _child_sees(env: ExportEnv, *editions: str) -> set[str]:
    with env.engine.connect() as c:
        return set(visible_edition_ids(c, CHILD, list(editions)))


def _suggest(env: ExportEnv, kind: str, entity: str, value: int, state: str = "open") -> None:
    with env.engine.begin() as c:
        c.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type=kind, entity_id=entity, value=value, x=False,
                provenance="provider", seen_at=NOW, state=state,
            )
        )  # fmt: skip


def test_counter_poc_a_chain_of_three_merges_undone_out_of_order_after_a_rating(
    fam: ExportEnv,
) -> None:
    """B2/B3 by a chain. m1: B into K, m2: C into K, m3: K into Z. The admin rates Z (holding all
    four Editions) 16. Undo m3, then m1, then m2. `_changed_since(m2)` looks for a `rate` of K or a
    later merge **into K**: the rating was of Z, and the 16 K got back from m3's undo is no audit
    line, so m2 is "untouched" and K and C get their 10 back from m2's snapshot."""
    env = fam
    k, b = _work(env, "Alpha Tale", 10, "A"), _work(env, "Alpha Tale (b)", 10, "B")
    c, z = _work(env, "Alpha Tale (c)", 10, "C"), _work(env, "Alpha Tale (z)", 10, "Z")
    m1 = _merge(env, k.work_id, b.work_id, NOW)
    m2 = _merge(env, k.work_id, c.work_id, NOW + H)
    m3 = _merge(env, z.work_id, k.work_id, NOW + 2 * H)
    _rate(env, z.work_id, 16, NOW + 3 * H)  # the admin read the whole and rated it 16
    eds = (k.edition_id, b.edition_id, c.edition_id, z.edition_id)
    assert _child_sees(env, *eds) == set()
    _separate(env, m3, NOW + 4 * H)
    assert _child_sees(env, *eds) == set()
    _separate(env, m1, NOW + 5 * H)
    assert _child_sees(env, *eds) == set()
    _separate(env, m2, NOW + 6 * H)
    assert _child_sees(env, *eds) == set(), "an undo lowered an age the admin raised on the whole"


def test_counter_poc_undoing_the_first_merge_settles_the_suggestion_a_later_merge_relied_on(
    fam: ExportEnv,
) -> None:
    """B1 by a chain. K holds a rejected 16, B and C each an open 16 (their only barrier). m1 opens
    K's row again (`_reopen_twin`) and deletes B's; m2 deletes C's (K's twin is already open, so
    nothing is noted). Undoing m1 sets K's row back to rejected: C's Edition, still under K, loses
    its barrier."""
    env = fam
    k, b = _work(env, "Alpha Tale", 10, "A"), _work(env, "Alpha Tale (b)", 10, "B")
    c = _work(env, "Alpha Tale (c)", 10, "C")
    _suggest(env, "work", k.work_id, 16, "rejected")
    _suggest(env, "work", b.work_id, 16)
    _suggest(env, "work", c.work_id, 16)
    assert _child_sees(env, b.edition_id, c.edition_id) == set()
    m1 = _merge(env, k.work_id, b.work_id, NOW)
    _merge(env, k.work_id, c.work_id, NOW + H)
    assert _child_sees(env, b.edition_id, c.edition_id) == set()
    _separate(env, m1, NOW + 2 * H)
    assert _child_sees(env, b.edition_id, c.edition_id) == set(), (
        "undoing m1 closed the open suggestion m2's Edition relied on"
    )


def test_counter_attack_an_open_suggestion_on_the_edition_survives_a_rating_and_the_undo(
    fam: ExportEnv,
) -> None:
    """Held: an open 16 on the absorbed Edition; the admin rates the merged Work 12 in between;
    the Edition stays hidden before and after « Séparer »."""
    env = fam
    k, b = _work(env, "Alpha Tale", 10, "A"), _work(env, "Alpha Tale (b)", 10, "B")
    _suggest(env, "edition", b.edition_id, 16)
    assert _child_sees(env, b.edition_id) == set()
    m1 = _merge(env, k.work_id, b.work_id, NOW)
    _rate(env, k.work_id, 12, NOW + H)
    assert _child_sees(env, b.edition_id) == set()
    _separate(env, m1, NOW + 2 * H)
    assert _child_sees(env, b.edition_id) == set()


def test_counter_attack_a_rating_at_the_merge_instant_counts(fam: ExportEnv) -> None:
    """Held: a rating stamped at the very instant of the merge counts as « since »."""
    env = fam
    k, b = _work(env, "Alpha Tale", 10, "A"), _work(env, "Alpha Tale (b)", 10, "B")
    m1 = _merge(env, k.work_id, b.work_id, NOW)
    _rate(env, k.work_id, 16, NOW)
    _separate(env, m1, NOW + H)
    assert _child_sees(env, k.edition_id, b.edition_id) == set()


def test_counter_attack_a_real_session_keeps_its_trace(settings: Settings, tmp_path: Path) -> None:
    """Held: a session identifier is a ULID (the `session` key), so `_ulid_or_none` keeps it, on
    the merge, the separation and the series plan."""
    with seeded(settings, tmp_path / "inst") as env:
        inst = env.inst
        ops = WorkMergeOps(inst.comp)
        sid = new_ulid()
        session = Principal(
            "u", "admin", "admin", Credential.SESSION, session_id=sid,
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
        sp = ops.series_reassign(keep.work_id, confirm=None, principal=session)
        ops.series_reassign(keep.work_id, confirm=sp["fingerprint"], principal=session)
        rows = list(env.rows(identity.audit))
        mine = {r.action: r.session_id for r in rows if r.action in
                {"work.merge", "work.separate", "series.reassign"}}  # fmt: skip
        assert mine == dict.fromkeys(("work.merge", "work.separate", "series.reassign"), sid)
        assert sum(r.action == "work.merge" for r in rows) == 1  # the plan read wrote nothing
