"""Relecture adverse of the mirror written once per series folder, and of the aliases filled once
per plan (ENF-PERF-007 against EF-EXP-063, EF-BIB-052 and L47).

Until this change, the mirror line of a file and the hashes of its alias were written right after
the commit of that file, inside `_place`, under the locks of the operation (`work`, the `path` of
the series folder, `library`). They are now written by `apply`, after the operation, with no lock
held. Each test below pins one consequence:

- a purge of a neighbour Unit takes the `path` lock of the folder, which nobody holds any more
  while the pipeline writes the mirror: the projection the pipeline read before the purge is
  written after it, and the purged Unit's line comes back, `unresolved`, for good;
- a failure after the commit of the first file of a folder skips its mirror and its alias, which
  `_place` used to write before anything else could fail;
- the hashes of an alias stay empty for the whole plan, and nothing in production fills an alias a
  stopped plan left empty: there is no sweep, and kosync looks an alias up by the very hash that
  is missing.
"""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.mirror import ProvenanceMirror
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.domain.physical_roots import RESERVED_DIR
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env
from tests.integration.test_purge_now import run as purge_run
from tests.purge_env import make_purge_env


def mirror_text(env: ExportEnv) -> str:
    folder = env.lib.library / RESERVED_DIR / "manifest"
    return "".join(path.read_text() for path in sorted(folder.rglob("*.jsonl")))


def item_of(env: ExportEnv, unit_id: str) -> str:
    with env.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(export.unit_coverage.c.carrier_id).where(
                    export.unit_coverage.c.carrier_type == "export_item",
                    export.unit_coverage.c.unit_id == unit_id,
                )
            ).scalar_one()
        )


def alias_hashes(env: ExportEnv) -> list[tuple[str, object, object]]:
    alias = export.document_alias
    with env.engine.connect() as connection:
        return [
            (str(r.export_item_id), r.koreader_partial_md5, r.kavita_md5)
            for r in connection.execute(sa.select(alias))
        ]


@pytest.fixture
def patch() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as context:
        yield context


@pytest.mark.req("EF-EXP-063", "EF-BIB-052")
def test_a_purge_between_the_projection_and_its_write_does_not_bring_the_unit_back(
    patch: pytest.MonkeyPatch,
) -> None:
    """Chapter 1 is exported, then chapter 2 is, and chapter 1 is purged while the pipeline writes
    the mirror of their folder. The purge holds the `path` lock of that folder: under the old
    code it could not get it while the pipeline wrote; now it does, and the pipeline writes the
    projection it made before the purge (chapter 1 still in the database). Once its rows are
    erased, the database no longer explains that line, so every later projection keeps it as
    `unresolved`: the Variant, the hashes and the export item of an erased document stay on disk."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series()
        first = env.add_chapter(series, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        first_item = item_of(env, first)
        assert first in mirror_text(env) or first_item in mirror_text(env)
        env.add_chapter(series, "2")

        real = ProvenanceMirror._put
        purged: list[bool] = []

        def purge_then_put(self: ProvenanceMirror, *args: Any, **kwargs: Any) -> Any:
            if not purged:  # the pipeline holds no lock here: the purge of chapter 1 goes in
                purged.append(purge_run(purge, first).complete)
            return real(self, *args, **kwargs)

        patch.setattr(ProvenanceMirror, "_put", purge_then_put)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        patch.undo()
        assert purged == [True]  # the purge ran to its end, its mirror cleaning included

        env.pipeline.heal_mirror(env.destination_id)  # the maintenance window does not help
        text = mirror_text(env)
        assert first not in text and first_item not in text, (
            "the line of a purged Unit was written back into the mirror by a projection made "
            "before the purge, outside the folder's lock, and is now kept as `unresolved`"
        )


@pytest.mark.req("EF-EXP-063", "EF-LEC-009")
def test_a_failure_after_the_commit_still_mirrors_and_aliases_the_file(
    patch: pytest.MonkeyPatch,
) -> None:
    """The file is placed and its line committed, then the operation fails (a lease lost when the
    write session closes, the volume's `note_placed`, a database error in `_keep_volume_opf`...).
    `_place` used to write the mirror and the alias hashes before anything after the commit could
    fail; now `apply` adds the folder to its due mirrors only when `_execute` returns, so the first
    file of a folder that fails this way leaves neither."""
    with export_env() as env:
        series = env.new_series()
        unit = env.add_chapter(series, "1")
        real = ExportPipeline._place

        def place_then_fail(self: ExportPipeline, *args: Any, **kwargs: Any) -> None:
            real(self, *args, **kwargs)
            raise RuntimeError("after the commit")

        patch.setattr(ExportPipeline, "_place", place_then_fail)
        with pytest.raises(RuntimeError):
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        patch.undo()
        assert len(env.files()) == 1  # placed, and its line committed
        item = item_of(env, unit)
        text = mirror_text(env) if (env.lib.library / RESERVED_DIR / "manifest").exists() else ""
        assert item in text or unit in text, "a file placed and committed has no mirror line"
        assert [h[1:] for h in alias_hashes(env) if h[0] == item] not in (
            [],
            [(None, None)],
        ), "a file placed and committed has an alias without hashes"


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_the_alias_of_a_placed_chapter_has_its_hashes_before_the_next_chapter(
    patch: pytest.MonkeyPatch,
) -> None:
    """L47: the hashes are filled after the commit of the file. They now wait for the end of the
    plan: a plan of five hundred chapters leaves every alias empty for its whole length, and a
    stop in it (SIGKILL, OOM, power cut) leaves them empty for good (see the test below)."""
    with export_env() as env:
        series = env.new_series()
        first = env.add_chapter(series, "1")
        env.add_chapter(series, "2")
        real = ExportPipeline._execute
        seen: list[list[tuple[str, object, object]]] = []

        def look_then_execute(self: ExportPipeline, *args: Any, **kwargs: Any) -> Any:
            seen.append(alias_hashes(env))
            return real(self, *args, **kwargs)

        patch.setattr(ExportPipeline, "_execute", look_then_execute)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        patch.undo()
        item = item_of(env, first)
        before_second = [h for h in seen[1] if h[0] == item]
        assert before_second and before_second[0][1] is not None, (
            "the alias of chapter 1 was still without hashes when chapter 2 was written"
        )


@pytest.mark.req("EF-LEC-009", "EF-LEC-014")
def test_aliases_a_stopped_plan_left_empty_are_filled_by_what_runs_next(
    patch: pytest.MonkeyPatch,
) -> None:
    """The plan stops before its end (the process is killed: `apply`'s `finally` never runs, which
    the patch stands for). What runs next — an export run with nothing to write, the maintenance
    healing of the mirror — never fills them: the "sweep" the docstrings rely on has no caller in
    production, and kosync finds an alias by the KOReader hash that is the one missing."""
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        patch.setattr(ExportPipeline, "_complete_alias_hashes", lambda self, ctx: None)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        patch.undo()
        assert alias_hashes(env) and all(h[1] is None for h in alias_hashes(env))

        env.pipeline.run(env.destination_id, job_id=new_ulid())  # after the restart
        env.pipeline.heal_mirror(env.destination_id)  # the maintenance window
        assert all(h[1] is not None and h[2] is not None for h in alias_hashes(env)), (
            "aliases left empty by a stopped plan are never filled again"
        )
