"""Discovery (PLAN L48): the translation type follows the declared nature, the sub-sources of the
service are kept, and a Variant is keyed by the stable upstream identifier.

Everything runs through the real follow cycle against the simulated Suwayomi: no row is written
to simulate the state under test.
"""

import pytest
import structlog

from shelfhold.catalog.public import translation_type_for
from tests.follow_env import follow_bench


@pytest.mark.req("EF-BIB-049", "EF-BIB-001")
def test_translation_type_for_follows_the_nature() -> None:
    assert translation_type_for("community") == "fan"
    assert translation_type_for("published") == "official"
    assert translation_type_for("machine") == "machine"
    assert translation_type_for("unknown") == "official"  # the kind decides the discriminant


@pytest.mark.req("EF-BIB-049")
def test_community_translation_relay_single_edition() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        alpha = bench.alpha()
        for chapter in alpha["chapters"]:
            chapter["scanlator"] = "Team A"
        bench.run()
        for number in range(3, 9):  # the series passes from one team to the next
            bench.add_chapter(number)
            alpha["chapters"][-1]["scanlator"] = "Team B"
        bench.run()
        editions = bench.rows("SELECT id, translation_type FROM edition")
        by_series = bench.rows(
            "SELECT e.id FROM edition e JOIN source_binding b ON b.edition_id = e.id"
            " JOIN source_item i ON i.id = b.source_item_id WHERE i.remote_id = '/series-alpha'"
        )
        assert len(by_series) == 1  # the relay did not open a second Edition
        assert {e["translation_type"] for e in editions} == {"fan"}
        edition_id = by_series[0]["id"]
        teams = bench.rows("SELECT label FROM team WHERE edition_id = :e", e=edition_id)
        assert {t["label"].casefold() for t in teams} == {"team a", "team b"}
        numbers = bench.rows(
            "SELECT number_norm FROM unit WHERE edition_id = :e ORDER BY source_order", e=edition_id
        )
        assert [n["number_norm"] for n in numbers] == [str(n) for n in range(1, 9)]


@pytest.mark.req("EF-BIB-049")
def test_unknown_translation_nature_is_logged_when_discovering() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.service.manifest = bench.service.manifest.model_copy(
            update={"translation_nature": "unknown"}
        )
        bench.cycle = bench.new_cycle()
        with structlog.testing.capture_logs() as logs:
            bench.run()
        told = [log for log in logs if log["event"] == "discovery.translation_nature_unknown"]
        assert told and told[0]["plugin_id"] == "suwayomi"
        assert bench.rows("SELECT count(*) AS n FROM edition")[0]["n"] >= 1


@pytest.mark.req("EF-SRC-021")
def test_suwayomi_three_sources_upserted_into_sub_source() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        rows = bench.rows(
            "SELECT source_key, name, lang, content_warning, limit_key, enabled FROM sub_source"
            " ORDER BY source_key"
        )
        assert [(r["source_key"], r["lang"], r["content_warning"]) for r in rows] == [
            ("suwayomi:7001", "en", "safe"),
            ("suwayomi:7002", "fr", "safe"),
            ("suwayomi:7003", "mul", "nsfw"),
        ]
        assert all(r["limit_key"] == r["source_key"] for r in rows)
        assert not any(r["enabled"] for r in rows)  # none is active on its own (ENF-LEG-001)
        bench.server.state["sources"][0]["displayName"] = "Renamed"
        bench.server.add_source("7004", "Fourth", "ja")
        bench.run()
        again = bench.rows("SELECT source_key, name FROM sub_source ORDER BY source_key")
        assert len(again) == 4 and again[0]["name"] == "Renamed"


@pytest.mark.req("EF-SRC-021")
def test_sub_sources_are_kept_when_nothing_is_left_to_discover() -> None:
    with follow_bench(seeded=True) as bench:  # the library is already bound: nothing to create
        bench.run()
        assert bench.rows("SELECT count(*) AS n FROM sub_source")[0]["n"] == 3


@pytest.mark.req("EF-SRC-036")
def test_remote_unit_id_stores_normalizer_and_raw() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        variants = bench.rows("SELECT remote_unit_id, remote_unit_id_norm_by FROM unit_variant")
        assert variants
        assert {v["remote_unit_id_norm_by"] for v in variants} == {"remote_unit_id@1"}
        urls = {c["url"] for c in bench.alpha()["chapters"]}
        stored = {v["remote_unit_id"] for v in variants}
        assert {f"suwayomi:7001:{u}" for u in urls} <= stored  # sourceId + chapter.url, as is
        assert not any(v["remote_unit_id"].rsplit(":", 1)[-1].isdigit() for v in variants)


@pytest.mark.req("EF-SRC-036")
def test_suwayomi_reinstall_keeps_variant_identity() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        bench.run()
        before = bench.rows("SELECT id, remote_unit_id FROM unit_variant ORDER BY id")
        assert before
        bench.server.reinstall()  # every local integer is new, the upstream identity is not
        bench.run()
        bench.run()
        after = bench.rows("SELECT id, remote_unit_id FROM unit_variant ORDER BY id")
        assert after == before  # no new Variant
        assert bench.rows("SELECT count(*) AS n FROM edition")[0]["n"] == 2
