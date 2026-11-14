"""M3 L57, contradicteur de la #257: the two production fixes slipped into the journeys.

(b) `0f485912` puts the application of a rename plan under the write session of its root. No
integration test saw the `LeaseLostError` it fixes: every rename test builds the pipeline with
`sessions=None` on an `Fsops` that does not enforce the lease (ENF-OPS-015), so the moves were
never asked for a lease. The first two tests are the missing red tests of (b): red on `449f5d42`
(the parent of the fix), green on the PR. The third is a hole the fix opens: the lease (and the
generation of every root, and the owed tombstone lines) is now taken before the refusals that
write nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
import sqlalchemy as sa

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.instance import WriteSessions
from shelfhold.infrastructure.schema import coherence
from tests.export_env import ExportEnv, export_env
from tests.rename_env import export_chapters, plan_status, record_paths, tree

LONGER = replace(DEFAULT_TEMPLATE, template_id="kavita-manga", version=2,
                 chapter="{series} - Chapter {number}")  # fmt: skip


@pytest.fixture
def env() -> Iterator[ExportEnv]:
    with export_env() as made:
        yield made


class CountingSessions(WriteSessions):
    """The real sessions; counts the leases taken (a take writes the lease file of the root)."""

    takes = 0

    def take(self, root_path: str) -> None:
        self.takes += 1
        super().take(root_path)


def _enforce(env: ExportEnv) -> CountingSessions:
    """From here on, as in production: `Fsops` refuses every write without our valid lease."""
    sessions = CountingSessions(
        env.lib.fsops, instance_id=env.lib.instance_id or new_ulid(), clock=env.clock
    )
    sessions.enforce()
    env.pipeline._sessions = sessions  # the `sessions=` the runtime gives (`session_opener`)
    return sessions


@pytest.mark.req("ENF-OPS-015", "EF-EXP-030")
def test_review_l57_poc_a_rename_plan_applies_with_the_lease_enforced(env: ExportEnv) -> None:
    export_chapters(env, "Dr Stone", ["1", "2"])
    _enforce(env)
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    assert preview.plan_id is not None
    env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
    assert tree(env) == [f"Dr Stone/Dr Stone - Chapter {n}.cbz" for n in "12"]
    assert record_paths(env) == tree(env)
    assert plan_status(env, preview.plan_id) == "done"


@pytest.mark.req("ENF-OPS-015", "EF-EXP-030")
def test_review_l57_poc_a_resumed_rename_plan_moves_under_the_lease(env: ExportEnv) -> None:
    """The job finds the plan `applying` (a stop): `resume` writes too, under the same lease."""
    export_chapters(env, "Dr Stone", ["1", "2"])
    _enforce(env)
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    assert preview.plan_id is not None
    with write_transaction(env.engine) as conn:
        conn.execute(
            sa.update(coherence.export_plan)
            .where(coherence.export_plan.c.plan_id == preview.plan_id)
            .values(status="applying")
        )
    env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
    assert tree(env) == [f"Dr Stone/Dr Stone - Chapter {n}.cbz" for n in "12"]
    assert plan_status(env, preview.plan_id) == "done"


@pytest.mark.req("ENF-OPS-014", "ENF-OPS-015")
def test_review_l57_poc_a_refused_rename_plan_takes_no_lease(env: ExportEnv) -> None:
    """A plan refused at once (412: the destination moved since the preview) writes nothing; it
    must not wake the disks for that: no lease file, no generation, no tombstone line. An export
    plan refused the same way takes no lease (its sessions open per write); since `0f485912` a
    rename plan takes the lease of every mounted root before it reads its own refusal."""
    export_chapters(env, "Dr Stone", ["1", "2"])
    sessions = _enforce(env)
    preview = env.pipeline.rename_preview(env.destination_id, template=LONGER)
    assert preview.plan_id is not None
    with pytest.raises(StalePlanError):
        env.pipeline.apply(preview.plan_id, if_match="not-the-version", job_id=new_ulid())
    assert sessions.takes == 0, "a refused rename plan took the lease of the root"
