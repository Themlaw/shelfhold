"""A shelf with exported chapters, for the tests of the rename plans (M3 L29).

Real files and a real database, like `export_env`; the readers of Kavita are a plain fake."""

from collections.abc import Sequence
from dataclasses import dataclass, field

import sqlalchemy as sa

from shelfhold.export.application.rename_plan import ReaderCount, RenameReport
from shelfhold.export.application.snapshot import load_context
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export
from tests.export_env import ExportEnv, Series


@dataclass
class FakeReaders:
    """What Kavita says about who reads a folder; `asked` keeps the folders it was asked about."""

    count: int = 0
    exact: bool = True
    asked: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, folders: Sequence[str]) -> ReaderCount:
        self.asked.append(tuple(folders))
        return ReaderCount(self.count, self.exact)


def export_chapters(env: ExportEnv, title: str, numbers: Sequence[str]) -> Series:
    series = env.new_series(title)
    for number in numbers:
        env.add_chapter(series, number)
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return series


def record_paths(env: ExportEnv) -> list[str]:
    return sorted(str(r.rel_path) for r in env.rows(export.export_record) if r.rel_path)


def tree(env: ExportEnv) -> list[str]:
    return sorted(str(p.relative_to(env.root)) for p in env.files())


def plan_status(env: ExportEnv, plan_id: str) -> str:
    with env.engine.connect() as conn:
        return str(
            conn.execute(
                sa.select(coherence.export_plan.c.status).where(
                    coherence.export_plan.c.plan_id == plan_id
                )
            ).scalar_one()
        )


def open_intents(env: ExportEnv) -> int:
    with env.engine.connect() as conn:
        return int(
            conn.execute(
                sa.select(sa.func.count())
                .select_from(coherence.fs_intent)
                .where(coherence.fs_intent.c.state.in_(("pending", "applying")))
            ).scalar_one()
        )


def item_ids_of(env: ExportEnv, folder: str) -> dict[str, str]:
    """`{file name: export item id}` of the written records under `folder`."""
    return {
        str(r.rel_path).split("/", 1)[1]: str(r.export_item_id)
        for r in env.rows(export.export_record)
        if r.rel_path and str(r.rel_path).split("/", 1)[0] == folder
    }


def resume_plan(env: ExportEnv, plan_id: str) -> RenameReport:
    """Go on with a plan a stop cut short, as the apply job does when it finds it `applying`,
    but through the book itself so that the report says what the settle did."""
    with env.engine.connect() as conn:
        destination = conn.execute(
            sa.select(coherence.export_plan.c.shelf_destination_id).where(
                coherence.export_plan.c.plan_id == plan_id
            )
        ).scalar_one()
        ctx = load_context(conn, destination)
    pipeline = env.pipeline
    return pipeline._renames.resume(
        plan_id, job_id=new_ulid(), versions=pipeline._versions(ctx),
        settle=lambda: pipeline._settle_renames(destination),
    )  # fmt: skip
