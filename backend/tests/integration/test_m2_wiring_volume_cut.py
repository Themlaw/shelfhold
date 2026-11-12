"""M2 wiring (L25, L26): the follow cycle cuts the novel of a source that declares no volumes.
The production stack of `lncrawl_bench` (adapter, guarded fetcher, keeper, follow cycle, a real
database): one ticket and one `export_item` per closed volume, the open tail waits under
`closed_only`, a closed volume is never asked again."""

from __future__ import annotations

import pytest

from tests.fakes.lncrawl import FakeLncrawl
from tests.lncrawl_env import lncrawl_bench

pytestmark = pytest.mark.req("EF-SRC-032", "EF-BIB-040")


def novel_without_volumes(count: int) -> FakeLncrawl:
    server = FakeLncrawl()
    server.novels = [
        {"id": "nv-alpha", "title": "Novel Alpha", "authors": ["Nobody"], "language": "en",
         "status": "ongoing",
         "chapters": [{"id": f"ch-{n}", "order": n, "title": f"Chapter {n}", "volume": None,
                       "closed": False, "team": "Synthetic Team A"} for n in range(1, count + 1)]}
    ]  # fmt: skip
    return server


def test_the_novel_is_cut_in_volumes_the_size_of_the_key_cap_and_the_open_tail_waits() -> None:
    with lncrawl_bench(novel_without_volumes(25)) as bench:
        bench.finish()
        tickets = bench.rows("SELECT target_kind, state FROM download_request")
        assert [t["target_kind"] for t in tickets] == ["range", "range"]
        assert {t["state"] for t in tickets} == {"fetched"}
        items = bench.rows(
            "SELECT id, kind, closed, json_extract(frozen_attrs, '$.volume_label') AS label "
            "FROM export_item ORDER BY label"
        )
        assert [(i["kind"], i["closed"], i["label"]) for i in items] == [
            ("volume", 1, "Vol.1"), ("volume", 1, "Vol.2"),
        ]  # fmt: skip
        spans = bench.rows("SELECT count(*) AS n FROM unit_coverage GROUP BY carrier_id")
        assert sorted(s["n"] for s in spans) == [10, 10]
        assert len(bench.server.jobs) == 2  # a job per volume, never one per chapter
        asked = {j["first"] for j in bench.server.jobs.values()}
        assert asked == {"ch-1", "ch-11"}
        assert len(bench.kept_files()) == 2
        bench.finish()  # a closed volume is never asked again
        assert len(bench.server.jobs) == 2 and bench.server.violations == []


def test_the_tail_is_asked_once_the_novel_is_finished_and_a_closed_volume_stays_frozen() -> None:
    with lncrawl_bench(novel_without_volumes(25)) as bench:
        bench.finish()
        frozen = {k.name: k.read_bytes() for k in bench.kept_files()}
        with bench.lib.engine.begin() as connection:
            import sqlalchemy as sa

            connection.execute(sa.text("UPDATE edition SET upstream_status = 'finished'"))
        bench.finish()
        items = bench.rows("SELECT closed, json_extract(frozen_attrs, '$.volume_label') AS l "
                           "FROM export_item ORDER BY l")  # fmt: skip
        assert [(i["l"], i["closed"]) for i in items] == [("Vol.1", 1), ("Vol.2", 1), ("Vol.3", 1)]
        assert {j["first"] for j in bench.server.jobs.values()} == {"ch-1", "ch-11", "ch-21"}
        assert all(
            frozen[name] == (bench.lib.data / "ranged" / name).read_bytes() for name in frozen
        )


def test_a_source_that_declares_its_volumes_keeps_them() -> None:
    with lncrawl_bench() as bench:  # the default novel: Vol.1 closed, Vol.2 open
        bench.finish()
        labels = bench.rows(
            "SELECT json_extract(frozen_attrs, '$.volume_label') AS l FROM export_item ORDER BY l"
        )
        assert [r["l"] for r in labels] == ["Vol.1", "Vol.2"]
        assert sorted(j["first"] for j in bench.server.jobs.values()) == ["ch-1", "ch-7"]
