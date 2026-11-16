"""M4 L27 (EF-BIB-014): the follow cycle after a migration, against the simulated service.

A series filed from one source moves to another that numbers its chapters 11, 12, ... for what the
library calls 1, 2, ... The migration is applied; the next cycles list the new source and must ask
for the chapters the library lacks and for nothing else: no chapter already filed is asked again,
no file is written beside the one that is there, and the library keeps its own numbering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from shelfhold.acquisition.public import SourceMigrations
from shelfhold.catalog.public import ListedUnit
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from tests.follow_env import Bench, follow_bench, new_chapter

pytestmark = pytest.mark.req("EF-BIB-014")

NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
OFFSET = 10  # the new source's 11 is the library's 1
FILED = (3, 4, 5, 6)  # chapters the library holds a file for (1 and 2 are known, not fetched)
NEW_SOURCE = "suwayomi:7002"


def admin() -> Principal:
    return Principal(
        user_id=new_ulid(), username="admin", role="admin", credential=Credential.LOCAL_CLI,
        can_rate=True,
    )


def beta(bench: Bench) -> dict[str, Any]:
    return next(m for m in bench.server.state["mangas"] if m["url"] == "/series-beta")


def publish_beta(bench: Bench, last: int) -> None:
    """The new source lists 11 .. `last` (the library's 1 .. last - 10)."""
    chapters = beta(bench)["chapters"]
    known = {c["chapterNumber"] for c in chapters}
    for number in range(OFFSET + 1, last + 1):
        if float(number) in known:
            continue
        chapter = new_chapter(6000 + number, number, f"/series-beta/ch-{number:03d}")
        chapter["sourceOrder"] = number - OFFSET - 1
        chapters.append(chapter)
    chapters[:] = [c for c in chapters if c["chapterNumber"] > OFFSET]  # drop the fixture's own


def settle(bench: Bench, times: int = 5) -> None:
    for _ in range(times):
        bench.run()


def test_source_migration_no_redownload_no_duplicate_file() -> None:
    with follow_bench() as bench:
        beta(bench)["inLibrary"] = False  # not yet the library of the service: nothing to discover
        bench.run()  # chapters 1 and 2 are known
        for number in FILED:
            bench.add_chapter(number)
        settle(bench)
        files = bench.library_files()
        assert len(files) == len(FILED)
        units_before = bench.rows("SELECT id, number_raw FROM unit ORDER BY number_sort")
        contents_before = bench.rows("SELECT id, content_hash FROM content ORDER BY id")
        asked_before = list(bench.server.enqueue_calls)

        # Alpha leaves the library of the service (the migration of the tool removes it) and the
        # new source arrives, listing 11 to 18: the library's 1 to 8
        bench.alpha()["inLibrary"] = False
        publish_beta(bench, OFFSET + 8)
        beta(bench)["inLibrary"] = True
        settle(bench, 2)
        assert not bench.binding()["active"]

        listed = tuple(
            ListedUnit(str(int(c["chapterNumber"])), c["sourceOrder"])
            for c in beta(bench)["chapters"]
        )
        migrations = SourceMigrations()
        from shelfhold.catalog.public import run_immediate

        def propose(c: Any) -> Any:
            return migrations.propose(
                c, bench.edition_id, plugin_id="suwayomi", source_key=NEW_SOURCE,
                remote_id="/series-beta", listed=listed, replaces_binding_id=bench.binding_id,
                principal=admin(), now=NOW,
            )  # fmt: skip

        proposal = run_immediate(bench.lib.engine, propose)
        assert proposal.offset == -OFFSET
        assert [r.number for r in proposal.to_fetch] == ["17", "18"]
        validated = run_immediate(
            bench.lib.engine,
            lambda c: migrations.validate(
                c, bench.edition_id, proposal.id, digest=proposal.digest, overrides=(),
                principal=admin(), now=NOW,
            ),
        )
        run_immediate(
            bench.lib.engine,
            lambda c: migrations.apply(
                c, bench.edition_id, proposal.id, digest=validated.digest, principal=admin(),
                now=NOW,
            ),
        )
        # no file, no Unit, no request exists for the new source yet
        assert bench.library_files() == files

        settle(bench, 6)  # the cycles list the new source, ask, download, file

        units = bench.rows("SELECT id, number_raw FROM unit ORDER BY number_sort")
        assert [u["number_raw"] for u in units] == [str(n) for n in range(1, 9)]  # 7 and 8: new
        assert units[:6] == units_before  # the six Units of the library are the very ones
        # not one chapter of the library was asked again: only the two it lacked were
        asked = bench.server.enqueue_calls[len(asked_before):]
        assert sorted(asked) == [6017, 6018]
        # one file per number: the six that were there, untouched, and two new ones
        after = bench.library_files()
        assert set(files) <= set(after) and len(after) == len(files) + 2
        assert len({p.name for p in after}) == len(after)
        contents = bench.rows("SELECT id, content_hash FROM content ORDER BY id")
        assert [c for c in contents if c in contents_before] == contents_before
        assert len(contents) == len(contents_before) + 2
        # the six held Units now also have the new source's release as another Variant (a record
        # of what it offers), and a Unit holds one file whatever the number of its Variants
        per_unit = bench.rows(
            "SELECT unit_id, count(*) AS n FROM content GROUP BY unit_id HAVING n > 1"
        )
        assert per_unit == []
        tickets = bench.rows(
            "SELECT v.remote_unit_id AS remote FROM download_request r "
            "JOIN unit_variant v ON v.id = r.variant_id "
            "JOIN source_binding b ON b.id = r.source_binding_id "
            "WHERE b.id <> :old ORDER BY v.remote_unit_id",
            old=bench.binding_id,
        )
        assert [t["remote"].rsplit("/", 1)[-1] for t in tickets] == ["ch-017", "ch-018"]
        # the numbering of the library was never touched by the numbers of the new source
        assert not bench.rows("SELECT 1 FROM unit WHERE CAST(number_norm AS REAL) > 8")


def test_a_chapter_the_new_source_publishes_later_lands_in_the_library_numbering() -> None:
    with follow_bench() as bench:
        beta(bench)["inLibrary"] = False
        bench.run()
        for number in FILED:
            bench.add_chapter(number)
        settle(bench)
        bench.alpha()["inLibrary"] = False
        publish_beta(bench, OFFSET + 6)
        beta(bench)["inLibrary"] = True
        settle(bench, 2)
        listed = tuple(
            ListedUnit(str(int(c["chapterNumber"])), c["sourceOrder"])
            for c in beta(bench)["chapters"]
        )
        from shelfhold.catalog.public import run_immediate

        migrations = SourceMigrations()

        def whole(c: Any) -> Any:
            proposal = migrations.propose(
                c, bench.edition_id, plugin_id="suwayomi", source_key=NEW_SOURCE,
                remote_id="/series-beta", listed=listed, replaces_binding_id=bench.binding_id,
                principal=admin(), now=NOW,
            )  # fmt: skip
            validated = migrations.validate(
                c, bench.edition_id, proposal.id, digest=proposal.digest, overrides=(),
                principal=admin(), now=NOW,
            )  # fmt: skip
            return migrations.apply(
                c, bench.edition_id, proposal.id, digest=validated.digest, principal=admin(),
                now=NOW,
            )  # fmt: skip

        run_immediate(bench.lib.engine, whole)
        settle(bench, 3)
        publish_beta(bench, OFFSET + 7)  # chapter 17 appears upstream: the library's 7
        settle(bench, 5)
        numbers = [u["number_raw"] for u in bench.rows("SELECT number_raw FROM unit")]
        assert sorted(numbers, key=float) == [str(n) for n in range(1, 8)]
        assert bench.server.enqueue_calls[-1] == 6017
