"""PoC of the counter-review of #249 (the export plan under the resource lock). Each test is red on
the branch of the PR and states what must hold once the objection is answered."""

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import OpenIntent
from shelfhold.export.application.locks import ResourceLocks, path_resource
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.ops.errors import StillWaitingError
from shelfhold.ops.intents import IntentOps
from tests.export_env import export_env

pytestmark = pytest.mark.req("EF-EXP-042")


def test_a_recycled_line_at_the_same_path_does_not_block_a_new_unit_forever() -> None:
    """B1: Ch.2 exported, then corrected to Ch.3 in the catalog (the file keeps its name until a
    rename plan), then deleted (its line `recycled`, the file in the bin, the line keeps its
    `rel_path`), then the real Ch.2 received. The planner plans it at the free path;
    `_superseded` matches the recycled line by `rel_path` and refuses the write, silently
    (REFUSED is no error), at every run. On `main` the file is written."""
    with export_env() as env:
        series = env.new_series()
        old = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        [written] = env.files()
        with env.engine.begin() as connection:
            connection.execute(  # the catalog correction
                sa.update(catalog.unit)
                .where(catalog.unit.c.id == old)
                .values(number_raw="3", number_norm="3", number_sort=3.0)
            )
            connection.execute(  # what `unit.delete` leaves behind
                sa.update(export.export_record).values(state="recycled")
            )
            connection.execute(  # and `drop_replicas` of the copy sent to the bin
                sa.delete(replica_schema.replica).where(
                    replica_schema.replica.c.export_item_id.is_not(None)
                )
            )
        written.unlink()
        new = env.add_chapter(series, "2", tag=b"again")
        report = env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert [op.unit_id for op in report.ops] == [new]  # red: [] (export.op_superseded)
        assert [p.name for p in env.files()] == ["Dr Stone - Ch.2.cbz"]
        states = sorted(r.state for r in env.rows(export.export_record))
        assert states == ["recycled", "written"]


def _rename(src: str, dst: str) -> Any:
    return OpenIntent(
        id=new_ulid(), op="rename", src=src, dst=dst, expected_sha256=None,
        expected_file_id=None, state="pending", epoch=0, schema_version="1",
    )  # fmt: skip


@pytest.mark.parametrize("kind", ["file", "dir"])
def test_a_rename_intent_across_two_folders_meets_a_writer_of_its_source_folder(kind: str) -> None:
    """M1: a rename plan (L29) holds the `path:` of both folders, old and new. `_path_lock` keeps
    only `dst`: an export writing in the source folder is not excluded while the intent is
    replayed or abandoned."""
    from types import SimpleNamespace

    with export_env() as env:
        ops = IntentOps(SimpleNamespace(engine=env.engine, clock=env.clock))  # type: ignore[arg-type]
        locks = ResourceLocks(env.engine, clock=env.clock)
        if kind == "file":
            intent = _rename(
                str(env.root / "Dr Stone" / "Dr Stone - Ch.2.cbz"),
                str(env.root / "Dr. Stone" / "Dr. Stone - Ch.2.cbz"),
            )
        else:
            intent = _rename(str(env.root / "Dr Stone"), str(env.root / "Dr. Stone"))
        source = path_resource(f"{env.destination_id}/Dr Stone")
        with (  # an export holding the source folder
            locks.hold_resources(new_ulid(), [source]),
            pytest.raises(StillWaitingError),  # red: DID NOT RAISE
            ops._held(intent),
        ):
            pass
