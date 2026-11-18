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
from shelfhold.reading.bundle.limits import BundleLimits
from shelfhold.reading.bundle.plan import Candidate, load_candidates
from shelfhold.reading.bundle.sources import ChapterLeftOutError, open_source
from tests.content_env import zip_of
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


def candidate_of(env: ExportEnv, edition: str) -> Candidate:
    with env.engine.connect() as connection:
        (candidate,) = load_candidates(connection, edition)
    return candidate


@pytest.mark.req("EF-EXP-058", "EF-BIB-045", "EF-INT-007")
def test_opds_pull_destination_serves_from_store_or_tree_copy() -> None:
    """The « store » of the card is the kept copy of the Unit (`/library`, the neutralized or
    plain file the core keeps), never the original an `untrusted` EPUB came from; the other
    source is the copy of a `tree` destination."""
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
        assert candidate_of(env, edition).left_out is None  # served, until it is damaged
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


@pytest.mark.req("EF-EXP-058", "EF-USR-006", "ENF-SEC-010")
def test_pull_item_is_read_only_from_a_unit_of_its_own_edition() -> None:
    """Nothing ties an `export_item` to the Unit it covers: one that was made to cover a Unit of
    another Edition (hidden from a child, say) serves nothing of it through this Edition's block."""
    with export_env(transport="pull") as env:
        edition, _unit = exported(env)
        other = env.new_series("Another Tale", shelf_id=env.shelf_id)
        elsewhere = env.add_chapter(other, "5")
        assert candidate_of(env, edition).left_out is None  # served before it is crossed
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.unit_coverage).values(unit_id=elsewhere))
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []


@pytest.mark.req("EF-EXP-058", "EF-INT-007")
def test_pull_item_covering_several_units_is_no_one_chapters_file() -> None:
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        second = env.add_chapter(env.main_series(), "78")
        assert candidate_of(env, edition).unit_id == unit
        with env.engine.begin() as connection:
            (item_id,) = connection.execute(sa.select(export.export_item.c.id)).scalars().all()[:1]
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=item_id, unit_id=second, position=2,
                    origin="manual",
                )
            )  # fmt: skip
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []  # not the first chapter's pages


@pytest.mark.req("EF-EXP-058", "EF-INT-007")
def test_pull_item_is_served_from_a_cbz_copy_of_its_own_content_only() -> None:
    """The copy is the registry's CBZ of the content the item names: another format of the Unit,
    or a copy whose identity is not the content's, is not what a `pull` client is given."""
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        assert candidate_of(env, edition).left_out is None
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE replica SET rel_path = rel_path || '.epub' WHERE unit_id = :u"),
                {"u": unit},
            )
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []  # an EPUB copy is no CBZ
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE replica SET rel_path = substr(rel_path, 1, length(rel_path) - 5)")
            )
            connection.execute(
                sa.text("UPDATE content SET content_hash = 'sha256:' || :h WHERE unit_id = :u"),
                {"h": "ab" * 32, "u": unit},
            )
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []  # not a copy of that content


@pytest.mark.req("EF-EXP-058", "EF-INT-007")
def test_pull_copy_replaced_after_the_registry_recorded_it_is_refused() -> None:
    """The candidate carries the registry's size and time: a file that is no longer that one is a
    block that is gone (nothing sent), as a tree copy replaced under the reader is."""
    from datetime import UTC, datetime

    from shelfhold.infrastructure.secrets.keyring import Keyring
    from shelfhold.reading.bundle import BundleGoneError, SeriesBundles

    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        candidate = candidate_of(env, edition)
        assert candidate.size is not None and candidate.mtime_ns is not None
        bundles = SeriesBundles(
            env.engine, lambda: Keyring.generate(), now=lambda: datetime(2026, 11, 18, tzinfo=UTC)
        )
        bundles._open_one(candidate).close()  # as recorded: opens
        kept_copy(env, unit).write_bytes(zip_of({"evil-001.png": b"\x89PNG\r\n\x1a\n"}))
        with pytest.raises(BundleGoneError):
            bundles._open_one(candidate)


@pytest.mark.req("EF-EXP-058", "EF-INT-007")
def test_pull_copy_is_opened_beneath_its_registered_root_not_through_a_swapped_folder() -> None:
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        candidate = candidate_of(env, edition)
        copy = kept_copy(env, unit)
        folder = copy.parent
        assert folder != Path(candidate.root or "")  # the root is the registry's, above the folder
        decoy = folder.parent / "decoy"
        decoy.mkdir()
        (decoy / copy.name).write_bytes(zip_of({"swapped-001.png": b"\x89PNG\r\n\x1a\n"}))
        folder.rename(folder.with_name(folder.name + ".moved"))  # the folder is now a link
        folder.symlink_to(decoy, target_is_directory=True)
        with pytest.raises(ChapterLeftOutError):
            open_source(candidate.root or "", candidate.rel_path)


@pytest.mark.req("EF-EXP-058", "EF-USR-006")
def test_pull_destination_never_serves_a_format_the_block_does_not_take() -> None:
    with export_env(transport="pull") as env:
        edition, unit = exported(env)
        assert candidate_of(env, edition).left_out is None  # a CBZ is served
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE content SET format = 'epub' WHERE unit_id = :u"), {"u": unit}
            )
        with env.engine.connect() as connection:
            assert load_candidates(connection, edition) == []  # an EPUB is no page of a block


def _limits() -> BundleLimits:
    return BundleLimits()
