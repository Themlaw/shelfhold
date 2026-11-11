"""M2 L25: the follow cycle over a real lncrawl adapter and the simulated instance.

EF-SRC-010, EF-SRC-032, EF-SRC-034, EF-BIB-049, EF-BIB-051: one ticket and one `export_item` per
range, a relay of teams stays one Edition, an unreachable instance pauses the service, and what
the instance sends is untrusted all the way: bounded, checked against what was asked, kept as it
came and neutralized before anything leaves.
"""

import hashlib
from pathlib import Path

import pytest

from shelfhold.acquisition.application.lncrawl_ranges import MAX_TICKET_AGE
from shelfhold.export.application.ranged_export import RangedExporter, RangedExportError
from shelfhold.sdk.testing.corpus.trapped_epubs import (
    base_traps,
    find_active_css,
    find_active_patterns,
    find_remote_references,
)
from tests.fakes.lncrawl import CANARY, FakeLncrawl
from tests.lncrawl_env import lncrawl_bench
from tests.ranged_env import ranged_bench, volume_epub

COUNT = "SELECT count(*) AS n FROM {table}"


def count(bench, table: str) -> int:  # type: ignore[no-untyped-def]
    return int(bench.rows(COUNT.format(table=table))[0]["n"])


@pytest.mark.req("EF-SRC-032", "EF-SRC-034", "EF-SRC-010")
def test_lncrawl_epub_per_range_single_export_item() -> None:
    with lncrawl_bench() as bench:
        bench.finish()
        tickets = bench.rows("SELECT target_kind, state, ticket FROM download_request")
        assert [t["target_kind"] for t in tickets] == ["range", "range"]  # a ticket per volume
        assert {t["state"] for t in tickets} == {"fetched"}
        items = bench.rows("SELECT id, kind, closed FROM export_item ORDER BY created_at, id")
        assert len(items) == 2 and sorted(i["closed"] for i in items) == [0, 1]
        spans = bench.rows(
            "SELECT carrier_id, count(*) AS n FROM unit_coverage GROUP BY carrier_id ORDER BY n"
        )
        assert [s["n"] for s in spans] == [4, 6]  # each Unit under one item, never two
        assert len(bench.server.jobs) == 2  # two jobs: one per range, not one per chapter
        # the EPUB the instance made is kept as it came (the original), one file per item
        kept = bench.kept_files()
        assert {p.stem for p in kept} == {i["id"] for i in items}
        assert {hashlib.sha256(p.read_bytes()).hexdigest() for p in kept} == {
            hashlib.sha256(volume_epub(n)).hexdigest() for n in (4, 6)
        }
        assert bench.server.violations == []


@pytest.mark.req("EF-BIB-049", "EF-SRC-034")
def test_lncrawl_team_relay_stays_one_edition() -> None:
    with lncrawl_bench() as bench:
        bench.finish()
        bench.server.add_chapter("nv-alpha", team="Synthetic Team C")  # the next team takes over
        bench.finish()
        editions = bench.rows("SELECT id, translation_type FROM edition")
        assert len(editions) == 1 and editions[0]["translation_type"] == "fan"
        teams = bench.rows("SELECT label FROM team WHERE edition_id = :e", e=editions[0]["id"])
        assert {t["label"].casefold() for t in teams} == {
            "synthetic team a", "synthetic team b", "synthetic team c",
        }  # fmt: skip
        assert count(bench, "work") == 1
        # the rolling volume grew: the same item, one more Unit, no third item
        assert count(bench, "export_item") == 2
        sizes = bench.rows("SELECT count(*) AS n FROM unit_coverage GROUP BY carrier_id ORDER BY n")
        assert [s["n"] for s in sizes] == [5, 6]


@pytest.mark.req("ENF-OPS-011", "EF-SRC-010")
def test_lncrawl_unreachable_pauses_service() -> None:
    with lncrawl_bench() as bench:
        bench.finish()
        bench.server.down = True
        for _ in range(3):
            bench.run()
        broken = bench.events("source.broken")
        assert broken and broken[0]["reason"] == "unreachable"  # `lncrawl_unreachable`
        assert count(bench, "export_item") == 2  # what was kept stays
        before = len(bench.server.jobs)
        bench.run()
        assert len(bench.server.jobs) == before  # a service that is down is not asked for more
        bench.server.down = False
        bench.run(2)  # back by itself: the next cycle is the probe
        assert bench.rows("SELECT count(*) AS n FROM download_request")[0]["n"] == 2


# --- attacks over the whole chain ------------------------------------------------------------
@pytest.mark.req("EF-SRC-033", "EF-SRC-034")
def test_lncrawl_queue_that_never_ends_is_cancelled_and_retried_a_bounded_number_of_times() -> None:
    server = FakeLncrawl()
    server.stuck = True
    with lncrawl_bench(server) as bench:
        bench.run(3)
        assert count(bench, "download_request") == 2 and count(bench, "export_item") == 0
        for _ in range(12):  # every time the ticket is older than the bound, it is let go
            bench.clock.advance(seconds=MAX_TICKET_AGE.total_seconds() + 60)
            bench.run(2)
        states = {r["state"] for r in bench.rows("SELECT state FROM download_request")}
        assert states == {"abandoned"}  # asked a bounded number of times, then left
        assert server.cancelled  # the instance was told to drop what it was holding
        assert count(bench, "export_item") == 0 and bench.kept_files() == []
        assert len(server.jobs) <= 2 * 8  # MAX_ATTEMPTS per range, never unbounded


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
def test_lncrawl_answer_for_another_range_keeps_nothing() -> None:
    server = FakeLncrawl()
    server.tamper_job = lambda a: (
        a | {"covers": [f"ch-{n}" for n in range(1, 11)]} if a["state"] == "done" else a
    )
    with lncrawl_bench(server) as bench:
        bench.finish()
        assert count(bench, "export_item") == 0 and bench.kept_files() == []
        assert server.artifact_hits == []  # not a byte of the file was asked for
        assert {r["state"] for r in bench.rows("SELECT state FROM download_request")} <= {
            "requested", "abandoned",
        }  # fmt: skip


@pytest.mark.req("EF-SRC-034", "EF-SRC-003", "ENF-SEC-009")
def test_lncrawl_file_redirected_to_another_host_keeps_nothing() -> None:
    server = FakeLncrawl()
    real = server.handle

    def redirecting(method, target, headers=None, body=b""):  # type: ignore[no-untyped-def]
        reply = real(method, target, headers, body)
        if "/artifacts/" in target and reply.status == 200:
            reply = reply.model_copy(
                update={"status": 302, "headers": {"location": "http://other.invalid/x.epub"},
                        "body": b""}
            )  # fmt: skip
        return reply

    server.handle = redirecting  # type: ignore[method-assign]
    with lncrawl_bench(server) as bench:
        bench.finish()
        assert count(bench, "export_item") == 0 and bench.kept_files() == []
        assert not [r for r in server.requests if "other.invalid" in r.path]


@pytest.mark.req("EF-SRC-010", "EF-BIB-051")
def test_lncrawl_file_that_is_not_an_epub_is_never_kept() -> None:
    server = FakeLncrawl()
    server.epub_override = b"<html><script>alert(1)</script></html>"
    with lncrawl_bench(server) as bench:
        bench.finish()
        assert count(bench, "export_item") == 0 and bench.kept_files() == []


@pytest.mark.req("EF-SRC-010", "ENF-SEC-009")
def test_lncrawl_api_key_and_crawler_name_never_reach_the_database_or_the_events() -> None:
    server = FakeLncrawl()
    server.leak_name = True
    with lncrawl_bench(server) as bench:
        bench.finish()
        dump = repr(
            [bench.rows(f"SELECT * FROM {t}") for t in
             ("download_request", "source_item", "source_binding", "unit", "unit_variant",
              "export_item", "unit_coverage", "team", "edition", "work")]
        ) + repr([e.payload for e in bench.delivered])  # fmt: skip
        assert CANARY not in dump
        from tests.fakes.lncrawl import API_KEY

        assert API_KEY not in dump
        assert all(
            r.headers.get("x-api-key") == API_KEY for r in server.requests
        )  # injected by the host on every call, including the file
        assert server.violations == []


# --- an EPUB that tries to get past the neutralizer ----------------------------------------------
@pytest.mark.req("EF-SRC-010", "EF-BIB-051", "ENF-SEC-015")
def test_lncrawl_epub_scripts_and_handlers_are_neutralized_before_export() -> None:
    hostile = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><body><script>alert(1)</script>'
        b'<img src="x" onerror="alert(1)"/><a href="javascript:alert(1)">x</a></body></html>'
    )
    server = FakeLncrawl()
    server.epub_override = _epub_with_chapter(hostile)
    with lncrawl_bench(server) as bench:
        bench.finish()
        kept = bench.kept_files()
        assert kept and all(p.read_bytes() == server.epub_override for p in kept)  # as it came
        item = kept[0].stem
        exporter = RangedExporter(bench.lib.engine, bench.lib.data, staging_dir=bench.lib.tmp)
        with exporter.prepare(item) as prepared:
            derived = prepared.path.read_bytes()
        assert b"<script" not in derived and b"onerror" not in derived
        assert b"javascript:" not in derived.lower()  # only the derivative can ever leave


@pytest.mark.req("EF-SRC-010", "EF-BIB-051", "ENF-SEC-015")
def test_lncrawl_every_trapped_epub_is_refused_or_clean_once_prepared() -> None:
    from tests.library_env import lib_env

    traps = base_traps()
    assert len(traps) >= 20
    with lib_env() as lib:
        bench = ranged_bench(lib, 1, declare_bounds=False)
        exporter = RangedExporter(lib.engine, lib.data, staging_dir=lib.tmp)
        for trap in traps:
            bench.kept().write_bytes(trap.epub)
            try:
                with exporter.prepare(bench.item_id) as prepared:
                    entries = _entries(prepared.path)
            except RangedExportError as refused:
                assert refused.code in {"ranged.untrusted_entry", "ranged.no_toc"}, trap.name
                continue
            findings = {
                n: (find_remote_references(c) if n.endswith(".opf")
                    else find_active_css(c) if n.endswith(".css") else find_active_patterns(c))
                for n, c in entries.items()
            }  # fmt: skip
            assert not {n: f for n, f in findings.items() if f}, trap.name
            assert bench.kept().read_bytes() == trap.epub  # the original is never rewritten


def _entries(path: Path) -> dict[str, bytes]:
    import zipfile

    with zipfile.ZipFile(path) as archive:
        return {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}


def _epub_with_chapter(chapter: bytes) -> bytes:
    """The synthetic volume, its first chapter replaced by `chapter`."""
    import io
    import zipfile

    source = zipfile.ZipFile(io.BytesIO(volume_epub(6)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for info in source.infolist():
            data = chapter if info.filename.endswith("ch1.xhtml") else source.read(info)
            archive.writestr(info, data)
    return out.getvalue()
