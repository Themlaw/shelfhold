"""L19: team policy, watermark, caps, backfill and follow suppression in the follow cycle.

Same bench as L18: the simulated Suwayomi, a real SQLite and a real /library.
"""

from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog.testing

from shelfhold.acquisition.public import (
    Backfill,
    BackfillConfirmationRequired,
    suppress_follow,
)
from shelfhold.catalog.public import Catalog
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import record_replica
from tests.follow_env import START, Bench, follow_bench, new_chapter
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.integration.test_schema_platform import add_user


def _ms(days_ago: float) -> str:
    return str(int((START - timedelta(days=days_ago)).timestamp() * 1000))


def _chapter(number: int, team: str, days_ago: float = 1.0) -> dict[str, Any]:
    chapter = new_chapter(
        9000 + number * 10 + ord(team) - 64, number, f"/series-alpha/ch-{number:03d}-{team}"
    )
    chapter["scanlator"] = f"Team {team}"
    chapter["uploadDate"] = _ms(days_ago)
    return chapter


def _serve(bench: Bench, chapters: list[dict[str, Any]]) -> None:
    bench.alpha()["chapters"][:] = chapters


def _watermark(bench: Bench, number: str) -> None:
    with bench.lib.engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE series_follow SET follow_from_segment = '', follow_from_number = :n, "
                "follow_from_norm_by = 'number@1'"
            ),
            {"n": number},
        )


def _settle(bench: Bench) -> None:
    """Cycles until what was asked is on disk (the simulated server needs a few polls)."""
    for _ in range(5):
        bench.run()
    assert not bench.rows("SELECT id FROM download_request WHERE state <> 'fetched'")


def _ticketed(bench: Bench) -> list[str]:
    """The chapter numbers that have a ticket, with the team of the Variant asked for."""
    return [
        f"{r['number_norm']}:{r['label']}"
        for r in bench.rows(
            "SELECT u.number_norm, lower(t.label) AS label FROM download_request r "
            "JOIN unit_variant v ON v.id = r.variant_id JOIN unit u ON u.id = v.unit_id "
            "LEFT JOIN team t ON t.id = v.team_id ORDER BY u.number_sort, t.label"
        )
    ]


@pytest.mark.req("EF-EXP-053")
def test_first_cycle_new_chapters_only_above_watermark() -> None:
    with follow_bench() as bench:
        _watermark(bench, "2")  # what the adoption recorded: chapters 1 and 2 are already there
        for number in (3, 4, 5, 6):
            bench.add_chapter(number)
        first = bench.run()
        assert first.requested == 4
        assert [t.split(":")[0] for t in _ticketed(bench)] == ["3", "4", "5", "6"]

    with follow_bench() as bench:  # no watermark yet: the series as it stands is the baseline
        for number in (3, 4, 5, 6):
            bench.add_chapter(number)
        assert bench.run().requested == 0
        assert bench.rows("SELECT follow_from_number AS n FROM series_follow")[0]["n"] == "6"
        bench.add_chapter(7)
        assert bench.run().requested == 1  # and from then on, only what is new


@pytest.mark.req("EF-EXP-053")
def test_requests_capped_per_cycle_and_limit_key() -> None:
    with follow_bench() as bench:
        _watermark(bench, "2")
        for number in range(3, 28):  # 25 chapters of one upstream source
            bench.add_chapter(number)
        assert bench.run().requested == 10  # ten per limit_key and per cycle
        assert len(bench.rows("SELECT id FROM download_request")) == 10
        assert bench.run().requested == 10  # the rest waits for the next cycle, in order
        assert bench.run().requested == 5
        assert bench.run().requested == 0
        numbers = [t.split(":")[0] for t in _ticketed(bench)]
        assert numbers == [str(n) for n in range(3, 28)]


@pytest.mark.req("EF-BIB-028")
def test_only_preferred_variant_enqueued() -> None:
    with follow_bench() as bench:
        held = [_chapter(n, "A", 30 - n) for n in (1, 2, 3)]
        _watermark(bench, "2")
        _serve(bench, held)
        bench.run()  # chapter 3 arrives...
        _settle(bench)  # ...and team A now holds a file on disk
        _serve(bench, [*held, _chapter(4, "A", 5), _chapter(4, "B", 4)])  # two teams offer 4
        assert bench.run().requested == 1
        assert _ticketed(bench) == ["3:team a", "4:team a"]  # the team already on disk, only


@pytest.mark.req("EF-BIB-028")
def test_single_variant_exported_per_unit() -> None:
    with follow_bench() as bench:
        _serve(bench, [_chapter(1, "A", 30), _chapter(1, "B", 29), _chapter(2, "B", 20)])
        bench.run()
        per_unit = bench.rows(
            "SELECT u.number_norm AS n, sum(v.preferred) AS preferred, count(*) AS variants "
            "FROM unit u JOIN unit_variant v ON v.unit_id = u.id WHERE u.edition_id = :e "
            "GROUP BY u.id ORDER BY u.number_sort",
            e=bench.edition_id,
        )
        assert [(r["n"], r["preferred"], r["variants"]) for r in per_unit] == [
            ("1", 1, 2),
            ("2", 1, 1),
        ]


@pytest.mark.req("EF-BIB-028")
def test_team_fallback_is_logged() -> None:
    with follow_bench() as bench:
        _watermark(bench, "0")
        _serve(bench, [_chapter(n, "B", 250 - n) for n in (1, 2, 3)])
        bench.run()
        _settle(bench)  # team B holds three files on disk
        _serve(
            bench,
            [_chapter(n, "B", 250 - n) for n in (1, 2, 3)]
            + [_chapter(4, "B", 240), _chapter(4, "A", 1)],  # B silent for 240 days, A fresh
        )
        with structlog.testing.capture_logs() as logs:
            bench.run()
        fallbacks = [entry for entry in logs if entry["event"] == "team.fallback"]
        assert len(fallbacks) == 1
        assert fallbacks[0]["reason"] == "majority_inactive"
        assert fallbacks[0]["number"] == "4"
        assert _ticketed(bench)[-1] == "4:team a"


@pytest.mark.req("EF-BIB-044")
def test_backfill_gaps_ignores_units_covered_by_volume() -> None:
    with follow_bench() as bench:
        _watermark(bench, "50")
        edition, binding = bench.edition_id, bench.binding_id
        with bench.lib.engine.begin() as connection:
            units = {}
            for n in range(1, 61):
                units[n] = add_unit(connection, edition, str(n))
                add_variant(connection, units[n], binding, f"synthetic:{n}")
            volume = _adopted_volume(connection, edition, [units[n] for n in range(1, 41)], bench)
            assert volume
            for n in (*range(41, 45), *range(46, 51)):  # chapter files; 45 is a hole
                record_replica(
                    connection, unit_id=units[n], location="library", content_hash=None,
                    independent=True, physical_root_id=bench.lib.library_root,
                    rel_path=f"ch-{n}.cbz",
                )  # fmt: skip
        backfill = Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=10**6)
        gaps = backfill.estimate(bench.edition_id, "gaps")
        assert gaps.units == 1  # only chapter 45: none of 1..40 (a volume has them), none beyond 50
        everything = backfill.estimate(bench.edition_id, "all")
        assert everything.units == 1


def _adopted_volume(
    connection: sa.Connection, edition_id: str, unit_ids: list[str], bench: Bench
) -> str:
    item_id = new_ulid()
    connection.execute(
        sa.insert(export.export_item).values(id=item_id, edition_id=edition_id, kind="adopted")
    )
    for position, unit_id in enumerate(unit_ids):
        connection.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="export_item", carrier_id=item_id, unit_id=unit_id,
                position=position, extent="full", origin="manual",
            )
        )  # fmt: skip
    record_replica(
        connection, unit_id=unit_ids[0], location="adopted_ro", content_hash=None,
        independent=True, physical_root_id=bench.lib.library_root, rel_path="vol-01.cbz",
        export_item_id=item_id,
    )  # fmt: skip
    return item_id


@pytest.mark.req("EF-EXP-053")
def test_backfill_requires_estimate_and_confirmation_over_100_units_or_5gib() -> None:
    with follow_bench() as bench:
        _watermark(bench, "150")
        with bench.lib.engine.begin() as connection:
            for n in range(1, 151):
                add_variant(
                    connection, add_unit(connection, bench.edition_id, str(n)), bench.binding_id,
                    f"synthetic:{n}",
                )  # fmt: skip
        backfill = Backfill(bench.lib.engine, free_bytes=lambda: 40 * 1024**3, unit_bytes=10**6)
        estimate = backfill.estimate(bench.edition_id, "all")
        assert (estimate.units, estimate.size_bytes, estimate.free_bytes) == (
            150, 150 * 10**6, 40 * 1024**3,
        )  # fmt: skip
        assert estimate.needs_confirmation
        with pytest.raises(BackfillConfirmationRequired) as raised:
            backfill.start(bench.edition_id, "all")  # nothing leaves without the confirmation
        assert raised.value.code == "backfill.confirmation_required"
        assert raised.value.estimate.units == 150
        assert bench.rows("SELECT backfill_mode AS m FROM series_follow")[0]["m"] == "none"
        started = backfill.start(
            bench.edition_id, "all", confirm_units=150, confirm_bytes=150 * 10**6
        )
        assert started.units == 150
        assert bench.rows("SELECT backfill_mode AS m FROM series_follow")[0]["m"] == "all"

    with follow_bench() as bench:  # few Units but heavy: 5 GiB is the other threshold
        _watermark(bench, "20")
        with bench.lib.engine.begin() as connection:
            for n in range(1, 21):
                add_variant(
                    connection, add_unit(connection, bench.edition_id, str(n)), bench.binding_id,
                    f"synthetic:{n}",
                )  # fmt: skip
        heavy = Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=300 * 1024**2)
        assert heavy.estimate(bench.edition_id, "all").needs_confirmation
        light = Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=10**6)
        assert not light.estimate(bench.edition_id, "all").needs_confirmation
        assert light.start(bench.edition_id, "all").units == 20  # under both: no confirmation


@pytest.mark.req("EF-EXP-053")
def test_a_confirmed_backfill_is_served_by_the_cycle_within_the_caps() -> None:
    with follow_bench() as bench:
        for number in range(3, 9):
            bench.add_chapter(number)
        assert bench.run().requested == 0  # baseline: 1..8 are known, none is requested
        backfill = Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=10**6)
        assert backfill.start(bench.edition_id, "all").units == 8
        assert bench.run().requested == 8
        assert [t.split(":")[0] for t in _ticketed(bench)] == [str(n) for n in range(1, 9)]
        bench.run()  # nothing left: the mode is closed by itself
        assert bench.rows("SELECT backfill_mode AS m FROM series_follow")[0]["m"] == "none"


@pytest.mark.req("EF-EXP-018")
def test_follow_suppressed_survives_source_restore() -> None:
    with follow_bench() as bench:
        assert bench.run().requested == 0  # baseline: units 1 and 2 are known, watermark 2
        unit_1 = bench.rows(
            "SELECT id FROM unit WHERE number_norm = '1' AND edition_id = :e", e=bench.edition_id
        )[0]["id"]
        with bench.lib.engine.begin() as connection:
            admin = add_user(connection)
        with bench.lib.engine.begin() as connection:
            suppress_follow(connection, unit_1, actor_id=admin, now=bench.clock.now())
        variant = bench.rows("SELECT id FROM unit_variant WHERE unit_id = :u", u=unit_1)[0]["id"]
        with bench.lib.engine.begin() as connection:  # the source loses the chapter...
            Catalog(connection).mark_removed(variant, bench.clock.now())
        original = [c for c in bench.alpha()["chapters"] if c["chapterNumber"] != 1.0]
        _serve(bench, [_chapter(1, "B", 3), *original])  # ...and gives it back, as a new release
        Backfill(bench.lib.engine, free_bytes=lambda: 10**12, unit_bytes=1).start(
            bench.edition_id, "all"
        )
        bench.run()
        assert [t.split(":")[0] for t in _ticketed(bench)] == ["2"]  # never the chapter 1
        row = bench.rows(
            "SELECT follow_suppressed AS s, follow_suppressed_by AS by FROM unit WHERE id = :u",
            u=unit_1,
        )[0]
        assert row["s"] == 1 and row["by"] == admin  # never lifted by itself, with its provenance
