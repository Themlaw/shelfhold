"""L31: `detach --restore-upstream` gives the series back to Suwayomi (EF-EXP-056).

The snapshot of what Suwayomi had decided (categories, update strategy) lives in its own table
and outlives the retention of the intent journal. Detaching gives the settings back, stops
Shelfhold's follow, lists what Shelfhold named, and never deletes or moves a file.
"""

import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.upstream import (
    Detacher,
    DetachScope,
    SnapshotRefusedError,
    UpstreamSnapshots,
    series_key,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.infrastructure.schema import acquisition
from shelfhold.sdk.refs import SeriesRef
from tests.adopt_env import tree_state
from tests.pairing_env import SERIES, Paired, paired_env

ALPHA = "/series-alpha"
CHAPTERS = [f"{SERIES} - Ch.{n}.cbz" for n in range(1, 51)]


def _paired(p: Paired) -> SeriesRef:
    p.adopt_files(CHAPTERS)
    p.upstream(51)
    p.a.adopt()
    (proposal,) = p.pairing.propose(p.destination, p.library())
    ref = proposal.candidates[0].ref
    p.pairing.confirm(p.destination, proposal.export_series_id, ref)
    return ref


def _taken(p: Paired, ref: SeriesRef) -> UpstreamSnapshots:
    """Alpha is in the category the doc recommends (left out of the scheduled update) and the
    snapshot of the other settings is taken before Shelfhold changes anything."""
    snapshots = UpstreamSnapshots(p.env.engine, clock=p.env.clock)
    keep = p.server.add_category("Reading")
    p.server.assign(ALPHA, keep)
    p.server.state["mangas"][0]["updateStrategy"] = "ONLY_FETCH_ONCE"
    assert asyncio.run(
        snapshots.take(p.service, p.fetcher, connected_service_id="suwayomi", ref=ref)
    )
    # then Shelfhold's own settings: excluded from the schedule, default strategy
    excluded = p.server.add_category("Shelfhold", include_in_update="EXCLUDE")
    p.server.assign(ALPHA, excluded)
    p.server.state["mangas"][0]["updateStrategy"] = "ALWAYS_UPDATE"
    return snapshots


@pytest.mark.req("EF-EXP-056")
def test_detach_restores_suwayomi_updates_and_leaves_files() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        before = tree_state(p.folder)
        records = p.rows("SELECT rel_path, file_hash FROM export_record ORDER BY rel_path")
        assert 101 not in p.server.run_scheduled_update()  # Suwayomi leaves the series alone
        report = asyncio.run(
            Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
                p.service, p.fetcher
            )
        )
        assert report.complete and [s.restored_from for s in report.series] == ["snapshot"]
        # the scheduled update now sees the series again, with the strategy it had
        assert p.server.categories_of(ALPHA) == [1]
        assert p.server.strategy_of(ALPHA) == "ONLY_FETCH_ONCE"
        p.server.state["mangas"][0]["updateStrategy"] = "ALWAYS_UPDATE"
        assert 101 in p.server.run_scheduled_update()
        # Shelfhold stopped following; the files are exactly where and what they were
        (binding,) = p.rows("SELECT active, disabled_reason FROM source_binding")[-1:]
        assert (binding["active"], binding["disabled_reason"]) == (0, "manual")
        assert p.settle() == 0 and p.downloads() == []  # no more chapter requested
        assert tree_state(p.folder) == before and report.deleted_files == 0
        assert p.rows("SELECT rel_path, file_hash FROM export_record ORDER BY rel_path") == records
        assert snapshots.count() == 0  # given back: the one way a snapshot goes
        # the folders and files Shelfhold named (none: everything here was adopted)
        assert report.managed == {}


@pytest.mark.req("EF-EXP-056")
def test_detach_lists_the_files_shelfhold_named_for_the_old_scripts() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        assert p.settle() == 1
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000001")
        report = asyncio.run(
            Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
                p.service, p.fetcher
            )
        )
        assert report.managed == {SERIES: [f"{SERIES} - Ch.51.cbz"]}


@pytest.mark.req("EF-EXP-056")
def test_upstream_snapshot_survives_intent_retention() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        journal = IntentJournal(p.env.engine, p.env.lib.fsops, clock=p.env.clock)
        old = journal.record(op="unlink", src=str(p.folder / "gone.cbz"))
        journal.mark_done(old)
        p.env.clock.advance(days=400)
        pruned = journal.prune(before=p.env.clock.now() - timedelta(days=30))
        assert pruned >= 1  # the retention of the journal ran
        assert snapshots.count() == 1 and snapshots.get("suwayomi", series_key(ref)) is not None
        report = asyncio.run(
            Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
                p.service, p.fetcher
            )
        )
        # the snapshot is found, not the default: the "Reading" category comes back
        assert [s.restored_from for s in report.series] == ["snapshot"]
        assert p.server.categories_of(ALPHA) == [1]


@pytest.mark.req("EF-EXP-056")
def test_snapshot_is_taken_once_and_holds_no_secret() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        again = asyncio.run(
            snapshots.take(p.service, p.fetcher, connected_service_id="suwayomi", ref=ref)
        )
        assert again is False  # the first one wins: a later one would record Shelfhold's own
        (row,) = p.rows("SELECT payload, schema_version, capability FROM upstream_snapshot")
        assert row["capability"] == "upstream_restore" and row["schema_version"] == 1
        from shelfhold.acquisition.application.upstream import validate_payload

        for bad in ({"password": "x"}, {"a": {"api_key": "x"}}, {"x": object()}):
            with pytest.raises(SnapshotRefusedError):
                validate_payload(bad)


@pytest.mark.req("EF-EXP-056")
def test_detach_without_snapshot_only_removes_the_exclusion() -> None:
    with paired_env() as p:
        ref = _paired(p)
        keep = p.server.add_category("Reading")
        p.server.assign(ALPHA, keep)
        excluded = p.server.add_category("Shelfhold", include_in_update="EXCLUDE")
        p.server.assign(ALPHA, excluded)
        snapshots = UpstreamSnapshots(p.env.engine, clock=p.env.clock)
        report = asyncio.run(
            Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
                p.service, p.fetcher
            )
        )
        assert [(s.ref.remote_id, s.restored_from) for s in report.series] == [
            (ref.remote_id, "default")
        ]
        assert p.server.categories_of(ALPHA) == [keep]  # the other category is left alone


@pytest.mark.req("EF-EXP-056")
def test_detach_keeps_the_follow_and_the_snapshot_when_suwayomi_fails() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        p.server.reinstall()  # every integer id changes: the cached ones are stale
        p.server.state["mangas"][0]["url"] = "/gone"  # and the series is gone from the library
        p.service.begin_cycle()
        report = asyncio.run(
            Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
                p.service, p.fetcher
            )
        )
        assert not report.complete and len(report.failed) == 1
        (binding,) = p.rows("SELECT active FROM source_binding")[-1:]
        assert binding["active"] == 1 and snapshots.count() == 1  # retried by the next run


@pytest.mark.req("EF-EXP-056")
def test_detach_scope_by_series_leaves_the_other_ones_followed() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        (edition,) = p.rows("SELECT edition_id FROM source_binding")[-1:]
        other = DetachScope(edition_id="0" * 26)
        report = asyncio.run(
            Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
                p.service, p.fetcher, other
            )
        )
        assert report.series == [] and snapshots.count() == 1
        assert edition["edition_id"] != other.edition_id
        assert p.rows("SELECT active FROM source_binding")[-1]["active"] == 1
        assert sa.select(acquisition.upstream_snapshot) is not None


def _restore(p: Paired, snapshots: UpstreamSnapshots) -> object:
    return asyncio.run(
        Detacher(p.env.engine, snapshots, connected_service_id="suwayomi").restore_upstream(
            p.service, p.fetcher
        )
    )


@pytest.mark.req("EF-EXP-056")
@pytest.mark.parametrize(
    "payload",
    [
        {"categories": [{"id": 1}], "update_strategy": "NOT_AN_ENUM; drop"},
        {"categories": [{"id": "x"}], "update_strategy": "ALWAYS_UPDATE"},
        {"categories": "abc", "update_strategy": 5},
    ],
)
def test_an_invalid_snapshot_counts_as_none_and_writes_nothing_wrong(payload: dict) -> None:  # type: ignore[type-arg]
    import json

    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        with p.env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE upstream_snapshot SET payload = :p"), {"p": json.dumps(payload)}
            )
        report = _restore(p, snapshots)
        assert [(s.restored_from, s.note) for s in report.series] == [  # type: ignore[attr-defined]
            ("default", "snapshot_invalid")
        ]
        assert p.server.strategy_of(ALPHA) == "ALWAYS_UPDATE" and p.server.violations == []


@pytest.mark.req("EF-EXP-056")
def test_only_the_exclusion_shelfhold_recorded_is_removed() -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        added = asyncio.run(
            snapshots.record_exclusion(
                p.service, p.fetcher, connected_service_id="suwayomi", ref=ref
            )
        )
        assert added == [2]
        mine = p.server.add_category("MyDropped", include_in_update="EXCLUDE")
        p.server.assign(ALPHA, mine)  # the admin's own exclusion, later
        report = _restore(p, snapshots)
        assert report.complete  # type: ignore[attr-defined]
        assert p.server.categories_of(ALPHA) == [1, mine]


@pytest.mark.req("EF-EXP-056")
def test_restoration_is_not_declared_done_while_the_exclusion_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        monkeypatch.setattr(p.server, "_q_updateMangaCategories", lambda q, v: {"manga": {}})
        report = _restore(p, snapshots)
        assert not report.complete  # type: ignore[attr-defined]
        assert snapshots.count() == 1 and p.rows("SELECT active FROM source_binding")[-1]["active"]


@pytest.mark.req("EF-SRC-033")
def test_update_patches_hold_only_the_documented_keys() -> None:
    """Contract: what the adapter sends to `updateManga` and `updateMangaCategories` (the fake
    refuses anything else, and `includeInUpdate` is never written)."""
    from shelfhold.plugin_host.adapters.meta.suwayomi import queries

    assert "patch: {updateStrategy: $strategy}" in queries.SET_UPDATE_STRATEGY
    assert "patch: {inLibrary: $inLibrary}" in queries.SET_LIBRARY
    assert "includeInUpdate" not in queries.SET_UPDATE_STRATEGY + queries.SET_MANGA_CATEGORIES
    with paired_env() as p:
        ref = _paired(p)
        snapshots = _taken(p, ref)
        _restore(p, snapshots)
        assert p.server.violations == []
        p.server._check_patch("patch: {includeInUpdate: EXCLUDE}", {"updateStrategy"})
        assert p.server.violations  # and the fake does notice a forbidden key


@pytest.mark.req("EF-EXP-056")
def test_take_missing_goes_on_after_a_failing_series() -> None:
    with paired_env() as p:
        ref = _paired(p)
        p.server.state["mangas"][0]["url"] = "/gone"
        p.service.begin_cycle()
        snapshots = UpstreamSnapshots(p.env.engine, clock=p.env.clock)
        report = asyncio.run(
            snapshots.take_missing(p.service, p.fetcher, connected_service_id="suwayomi")
        )
        assert report.taken == 0 and [r.remote_id for r, _ in report.failed] == [ref.remote_id]
