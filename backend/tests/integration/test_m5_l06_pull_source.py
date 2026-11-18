"""M5 L06 (EF-EXP-058, volet M5): a `pull` binding serves only from a readable copy.

A `pull` destination (OPDS, the kosync of Shelfhold) has no file of its own: its manifest line has
no locator. What a client downloads comes from a copy the core can read: the file of a co-linked
`tree` destination, or else the kept copy of the Unit (`/library`, never an original of `/store`
that an `untrusted` EPUB came from). A binding with neither serves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.reading.bundle.plan import load_candidates
from shelfhold.reading.bundle.sources import open_source
from tests.export_env import ExportEnv, export_env


def exported(env: ExportEnv) -> tuple[str, str]:
    series = env.main_series()
    unit = env.add_chapter(series, "77")
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    return series.edition_id, unit


def kept_copy(env: ExportEnv, unit: str) -> Path:
    from shelfhold.library.replica import library_copies

    with env.engine.connect() as connection:
        (copy, *_rest) = library_copies(connection, unit)
    return Path(copy.local)


@pytest.mark.req("EF-EXP-058", "EF-BIB-045", "EF-INT-007")
def test_opds_pull_destination_serves_from_store_or_tree_copy() -> None:
    # a pull destination alone: no file of its own, the kept copy is what a client downloads
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        (line,) = env.rows(export.export_record)
        assert (line.locator_type, line.state) == ("none", "written") and env.files() == []
        with env.engine.connect() as connection:
            (candidate,) = load_candidates(connection, edition)
        assert candidate.left_out is None
        assert Path(candidate.root or "", candidate.rel_path) == kept_copy(env, unit)
        opened = open_source(candidate.root or "", candidate.rel_path)
        try:
            assert [p.name for p in opened.scan(_limits()).pages] == ["77-001.png"]
        finally:
            opened.close()
    # the same Unit on a `tree` destination: the tree copy, as before, and not the kept one
    with export_env(transport="tree") as env:
        edition, unit = exported(env)
        (tree_file,) = env.files()
        with env.engine.connect() as connection:
            (candidate,) = load_candidates(connection, edition)
        assert Path(candidate.root or "", candidate.rel_path) == tree_file
        assert tree_file != kept_copy(env, unit)


@pytest.mark.req("EF-EXP-058", "EF-INT-007")
def test_pull_destination_without_readable_copy_serves_nothing() -> None:
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        with env.engine.begin() as connection:  # the integrity check found the kept copy damaged
            connection.execute(
                sa.text("UPDATE replica SET damaged_at = CURRENT_TIMESTAMP WHERE unit_id = :u"),
                {"u": unit},
            )
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []
        with env.engine.begin() as connection:  # and a Unit that no pull line names is no block
            connection.execute(sa.delete(export.export_record))
            connection.execute(sa.text("UPDATE replica SET damaged_at = NULL"))
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []


@pytest.mark.req("EF-EXP-058", "EF-USR-006")
def test_pull_destination_never_serves_a_format_the_block_does_not_take() -> None:
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        with env.engine.begin() as connection:
            connection.execute(sa.text("UPDATE content SET format = 'epub' WHERE unit_id = :u"), {"u": unit})
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []  # an EPUB is no page of a block


def _limits() -> object:
    from shelfhold.reading.bundle.limits import BundleLimits

    return BundleLimits()
