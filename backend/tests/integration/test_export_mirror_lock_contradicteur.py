"""Relecture adverse, second pass: the mirror under the folder's lock, the sweep of the aliases.

`ProvenanceMirror._put` now takes the `path` lock of the series folder itself, under a job of its
own. Two consequences are pinned here:

- the same-path upgrade (and the volume assembly) call `sync_destination` while they hold that
  very lock under their own job: `_put` waits for it, gives up after `MIRROR_LOCK_WAIT_S`, and the
  mirror of the folder they just rewrote is not written (it was, before);
- the sweep of the aliases added to a run with nothing to write opens the files of the tree,
  where that run promised to read no disk at all (ENF-OPS-014, EF-EXP-029).
"""

import time
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.upgrade import UpgradeOutcome
from shelfhold.export.domain.physical_roots import RESERVED_DIR
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env
from tests.fs_spy import FsSpy
from tests.upgrade_env import better_copy, export_one, record_of, request, service


def mirror_text(env: ExportEnv) -> str:
    folder = env.lib.library / RESERVED_DIR / "manifest"
    return "".join(path.read_text() for path in sorted(folder.rglob("*.jsonl")))


@pytest.mark.req("EF-EXP-063")
def test_a_same_path_upgrade_writes_the_mirror_of_its_folder() -> None:
    """The upgrade holds `path:{destination}/{folder}` while it calls the mirror; the mirror asks
    for the same lock under another job and waits for itself until it gives up."""
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)
        started = time.monotonic()
        result = service(env).upgrade(request(env, exported, content_id))
        took = time.monotonic() - started
        assert result.outcome is UpgradeOutcome.UPGRADED
        new_hash = str(record_of(env, exported).file_hash)
        assert new_hash != exported.old_file_hash
        assert new_hash in mirror_text(env), (
            f"the mirror still names the replaced file after the upgrade (the upgrade took "
            f"{took:.1f} s: the mirror waited for the lock its own caller holds)"
        )


@pytest.mark.req("ENF-OPS-014", "EF-EXP-029")
def test_a_run_with_nothing_to_write_still_reads_no_disk_with_an_alias_left_empty(
    patch: pytest.MonkeyPatch,
) -> None:
    """An alias a stopped plan left empty: the next run with nothing to write must not open the
    tree for it (the disks sleep); the maintenance window is the place for that sweep."""
    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        patch.setattr(ExportPipeline, "_complete_alias_hashes", lambda self, ctx: None)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        patch.undo()
        with env.engine.connect() as connection:
            empty = connection.execute(
                sa.select(export.document_alias.c.export_item_id).where(
                    export.document_alias.c.koreader_partial_md5.is_(None)
                )
            ).all()
        assert empty  # the state a stopped plan leaves

        with FsSpy([env.root]).watching() as spy:
            assert env.pipeline.run(env.destination_id, job_id=new_ulid()).ops == ()
        assert spy.accesses == [], spy.accesses


@pytest.fixture
def patch() -> Any:
    with pytest.MonkeyPatch.context() as context:
        yield context
