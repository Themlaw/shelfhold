"""EF-EXP-042 / ENF-QUAL-006 (I10): a plan carries `base_version`; a stale one is refused (412)."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export import errors as export_errors
from shelfhold.export.application.plans import PlanOp, PlanStore, StalePlanError
from shelfhold.infrastructure.schema import coherence
from tests.intents_env import IntentEnv, make_intent_env


@dataclass
class Counter:
    """Stand-in for the version counter of the Units, manifest and bindings (02 section 6.4)."""

    value: int = 7


@dataclass
class Preview:
    env: IntentEnv
    counter: Counter
    store: PlanStore
    file: Path
    plan_id: str
    base_version: str
    ran: list[str] = field(default_factory=list)


@pytest.fixture
def preview(tmp_path: Path) -> Preview:
    ienv = make_intent_env(tmp_path)
    counter = Counter()
    store = PlanStore(ienv.engine, clock=ienv.clock, current_version=lambda: str(counter.value))
    file = ienv.fs.manga / "Series" / "1.cbz"
    ienv.fs.fsops.write_atomic(str(file), b"chapter", parents=True)
    renamed = file.with_name("1 renamed.cbz")
    plan = store.create(
        kind="rename_plan",
        files=[str(file)],
        ops=[PlanOp("rename", src=str(file), dst=str(renamed))],
    )
    return Preview(ienv, counter, store, file, plan.plan_id, plan.base_version)


def plan_status(p: Preview) -> str:
    with p.env.engine.connect() as conn:
        return conn.execute(
            sa.select(coherence.export_plan.c.status).where(
                coherence.export_plan.c.plan_id == p.plan_id
            )
        ).scalar_one()


@pytest.mark.req("EF-EXP-042")
def test_plan_records_base_version_and_fingerprint(preview: Preview) -> None:
    assert preview.base_version == "7"
    with preview.env.engine.connect() as conn:
        row = conn.execute(sa.select(coherence.export_plan)).one()
        ops = conn.execute(sa.select(coherence.export_op)).all()
    assert row.base_version == "7"
    assert row.files_fingerprint.startswith("sha256:")
    assert row.status == "planned"
    assert [(o.op, o.status) for o in ops] == [("rename", "pending")]


@pytest.mark.req("EF-EXP-042", "ENF-QUAL-006")
def test_stale_plan_refused_412(preview: Preview) -> None:
    """A chapter arrived between the preview and the click: 412 and nothing is applied."""
    preview.counter.value += 1
    with pytest.raises(StalePlanError) as raised:
        preview.store.apply(preview.plan_id, if_match=preview.base_version, run=preview.ran.append)
    assert raised.value.code == "plan.stale"
    assert raised.value.status == 412
    assert preview.ran == []
    assert plan_status(preview) == "stale"
    assert preview.file.exists()


@pytest.mark.req("EF-EXP-042")
def test_stale_plan_status_is_412_not_409_in_the_catalog() -> None:
    entry = next(c for c in export_errors.CODES if c.code == "plan.stale")
    assert entry.status == 412  # 409 stays for business conflicts
    assert entry.audience == "user"


@pytest.mark.req("EF-EXP-042")
def test_plan_refused_when_a_file_changed_under_it(preview: Preview) -> None:
    """Same version counter, but the file moved on: stale."""
    preview.env.fs.fsops.write_atomic(str(preview.file), b"a different, longer chapter")
    with pytest.raises(StalePlanError):
        preview.store.apply(preview.plan_id, if_match=preview.base_version, run=preview.ran.append)
    assert preview.ran == []


@pytest.mark.req("EF-EXP-042")
def test_if_match_of_an_older_preview_is_refused(preview: Preview) -> None:
    with pytest.raises(StalePlanError):
        preview.store.apply(preview.plan_id, if_match="6", run=preview.ran.append)
    assert preview.ran == []


@pytest.mark.req("EF-EXP-042")
def test_fresh_plan_is_applied_op_by_op(preview: Preview) -> None:
    preview.store.apply(
        preview.plan_id, if_match=preview.base_version, run=lambda op: preview.ran.append(op.op)
    )
    assert preview.ran == ["rename"]
    assert plan_status(preview) == "done"
    with preview.env.engine.connect() as conn:
        assert {r.status for r in conn.execute(sa.select(coherence.export_op))} == {"done"}
