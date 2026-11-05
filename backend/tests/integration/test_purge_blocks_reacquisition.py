"""EF-BIB-052, EF-BIB-053: what was purged does not come back (L34). The follow cycle, "fill the
gaps" and the import all ask the tombstones first; a re-uploaded chapter at the same place under a
new identifier is stopped by the `place` scope. Real follow cycle over the simulated Suwayomi."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import PassthroughStore, TombstonedContentError
from tests.follow_env import Bench, follow_bench, new_chapter
from tests.library_env import LibEnv
from tests.purge_env import ADMIN, PurgeEnv, make_purge_env

pytestmark = pytest.mark.req("EF-BIB-052", "EF-BIB-053")
SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic" / "comic_a_002.cbz"


def _units(bench: Bench) -> list[str]:
    return [
        r["number_raw"]
        for r in bench.rows(
            "SELECT number_raw FROM unit WHERE edition_id = :e ORDER BY number_sort", e=bench.edition_id
        )
    ]


def test_tombstone_blocks_fill_gaps_and_reimport() -> None:
    holder: list[PurgeEnv] = []

    def guard(lib: LibEnv) -> object:
        holder.append(make_purge_env(lib))
        return holder[0].tombstones

    with follow_bench(tombstones=guard) as bench:
        purge = holder[0]
        bench.run()  # baseline: chapters 1 and 2
        bench.add_chapter(3)
        bench.run()  # chapter 3 is new: requested, fetched, filed
        (unit,) = [
            r["id"] for r in bench.rows("SELECT id FROM unit WHERE number_raw = '3'")
        ]
        assert bench.library_files()
        purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=purge.clock.now())
        assert _units(bench) == ["1", "2"] and bench.library_files() == []

        # "fill the gaps" and the follow cycle see chapter 3 upstream again: nothing is created
        with bench.lib.engine.begin() as connection:
            connection.execute(
                sa.update(acquisition.series_follow).values(backfill_mode="gaps")
            )
        report = bench.run()
        assert report.discovered == 0 and report.requested == 0
        assert _units(bench) == ["1", "2"]
        assert bench.rows("SELECT id FROM download_request") == [] or all(
            r["state"] == "closed_by_purge" for r in bench.rows("SELECT state FROM download_request")
        )
        assert bench.library_files() == []

        # the same file arriving by another road (import): refused on its content alone
        store = PassthroughStore(
            bench.lib.engine, bench.lib.fsops, bench.lib.locate, bench.lib.data,
            tombstones=purge.tombstones,
        )  # fmt: skip
        other_unit, other_variant = bench.lib.new_unit()
        staged = bench.lib.stage(SYNTHETIC.read_bytes())  # the bytes the fake served for chapter 3
        with pytest.raises(TombstonedContentError):
            store.ingest(
                unit_id=other_unit, variant_id=other_variant, staged=staged,
                dest=str(bench.lib.manga / "Again" / "ch.cbz"),
            )  # fmt: skip
        assert not (bench.lib.manga / "Again").exists()  # nothing was placed


def test_reuploaded_chapter_blocked_by_place_tombstone() -> None:
    holder: list[PurgeEnv] = []

    def guard(lib: LibEnv) -> object:
        holder.append(make_purge_env(lib))
        return holder[0].tombstones

    with follow_bench(tombstones=guard) as bench:
        purge = holder[0]
        bench.run()
        bench.add_chapter(3)
        bench.run()
        (unit,) = [r["id"] for r in bench.rows("SELECT id FROM unit WHERE number_raw = '3'")]
        purge.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=purge.clock.now())

        # the source deletes chapter 3 and uploads it again under a new identifier, same place
        chapters = bench.alpha()["chapters"]
        chapters[:] = [c for c in chapters if c["chapterNumber"] != 3.0]
        chapters.append(new_chapter(9003, 3, url="/series-alpha/ch-003-reupload"))
        report = bench.run()

        assert report.discovered == 0 and report.requested == 0
        assert _units(bench) == ["1", "2"]  # no Unit at that place, no Variant, no file
        assert bench.library_files() == []
        assert bench.rows("SELECT id FROM unit_variant WHERE remote_unit_id LIKE '%reupload'") == []
