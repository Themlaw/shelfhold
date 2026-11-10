"""ENF-OPS-011: the failure matrix (spec section 7 bis) replayed deterministically.

Each row has its event (EF-INT-006), the error code of the same identifier (EF-UX-003) and, when a
human has to act, an exit command (EF-INT-023)."""

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from shelfhold.error_catalog import ErrorCode
from shelfhold.events.operating import NOT_NOTIFIED, OPERATING_EVENTS, WAITING_STATE_EVENT
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from shelfhold.i18n import load_catalog
from shelfhold.infrastructure.fsops.probe import probe_folder
from shelfhold.infrastructure.observability.failure_matrix import (
    FAILURE_MATRIX,
    M1A_BEHAVIOURS,
    rows_for,
)
from tests.fakes_clock import ManualClock
from tests.faults.simulator import FaultSimulator
from tests.fsops_env import make_env
from tests.kavita_env import NOW, RecordingEvents


def all_codes() -> dict[str, ErrorCode]:
    from shelfhold.interfaces.http_api.catalog import all_error_codes

    return {c.code: c for c in all_error_codes()}


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_events_and_codes_consistent() -> None:
    codes, texts = all_codes(), load_catalog()
    assert len({row.key for row in FAILURE_MATRIX}) == len(FAILURE_MATRIX)
    for row in FAILURE_MATRIX:
        assert row.event in OPERATING_EVENTS, row.key
        assert row.event in codes and codes[row.event].event, f"{row.event}: code of the same name"
        assert texts.get(codes[row.event].title_key) and texts.get(codes[row.event].action_key)
        if row.needs_human and row.event not in NOT_NOTIFIED:
            assert row.exit_command, f"{row.key}: a human must act, so a command must exist"
        assert row.behaviour in M1A_BEHAVIOURS or row.milestone != "M1a", row.key
    covered = {row.event for row in FAILURE_MATRIX}
    # every state in which Shelfhold waits for a human announces itself with a matrix event
    assert set(WAITING_STATE_EVENT.values()) <= covered
    assert {"disk.low", "mount.sentinel_missing", "instance.lease_conflict"} <= covered
    assert {r.key for r in rows_for("M1a")} >= {
        "disk_low",
        "kavita_unreachable",
        "suwayomi_unreachable",
    }


@pytest.mark.req("ENF-OPS-011")
def test_kavita_down_pauses_exports() -> None:
    """Kavita unreachable: files stay written, scans wait, one notification, back by itself."""
    simulator = FaultSimulator()
    fetcher, events = simulator.dead_fetcher(), RecordingEvents()
    client = KavitaClient(
        fetcher, "http://kavita.test", AdminKey("k" * 16), events=events, clock=ManualClock(NOW)
    )

    async def ask() -> None:
        await client.probe()

    for _ in range(3):
        with pytest.raises(TargetUnreachable):
            asyncio.run(ask())
    assert events.codes() == ["target.unreachable"]  # announced once, not once per attempt
    assert not client.health.paused  # no destination disabled: the key was not revoked
    assert client.health.waiting_state is None or client.health.waiting_state != "target_forbidden"
    row = next(r for r in FAILURE_MATRIX if r.key == "kavita_unreachable")
    assert row.behaviour == "pause_exports" and row.event == "target.unreachable"


@pytest.mark.req("ENF-OPS-011")
def test_suwayomi_down_marks_source_unavailable() -> None:
    from shelfhold.sdk.source import TemporarilyUnavailable
    from tests.follow_env import follow_bench

    with follow_bench() as bench:
        bench.run()
        original = bench.service.followed_series

        async def down(fetcher: Any) -> Any:
            raise TemporarilyUnavailable("connection refused")

        bench.service.followed_series = down  # type: ignore[method-assign]
        for _ in range(2):
            assert not bench.run().unavailable  # a blip is not an outage
        assert bench.events("source.broken") == []
        report = bench.run()  # the third cycle in a row crosses the threshold
        assert report.unavailable
        assert [e["reason"] for e in bench.events("source.broken")] == ["unreachable"]
        assert bench.run().unavailable and len(bench.events("source.broken")) == 1
        assert bench.rows("SELECT * FROM source_failure") == []  # nothing counted as content
        assert bench.binding()["active"]  # nothing deactivated, nothing safeguarded

        bench.service.followed_series = original  # the probe answers again: automatic return
        back = bench.run()
        assert not back.unavailable and back.discovered >= 0
        assert bench.run().paused is None


@pytest.mark.req("EF-UX-002", "ENF-OPS-011")
def test_mount_probe_write_and_yaml_hint_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = make_env(tmp_path)
    folder = str(made.manga)
    ok = probe_folder(made.fsops, folder)
    assert ok.ok and ok.steps == ("write", "rename", "mkdir")
    assert not [p for p in made.manga.iterdir() if p.name.startswith(".shelfhold-probe-")]

    def refused(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(made.fsops, "write_atomic", refused)
    bad = probe_folder(made.fsops, folder)
    assert not bad.ok and bad.failed_step == "write" and bad.event == "target.permission_denied"
    owner = made.manga.stat()
    assert f'user: "{owner.st_uid}:{owner.st_gid}"' in bad.yaml_hint  # the exact block to paste
    assert "volumes:" in bad.yaml_hint and f"{folder}:" in bad.yaml_hint
    assert bad.cause  # says why, not only that it failed


# -- the replay of every simulable row ---------------------------------------------------------


def _kavita_unreachable(tmp: Path) -> str:
    events = RecordingEvents()
    client = KavitaClient(
        FaultSimulator().dead_fetcher(), "http://kavita.test", AdminKey("k" * 16),
        events=events, clock=ManualClock(NOW),
    )  # fmt: skip
    with pytest.raises(TargetUnreachable):
        asyncio.run(client.probe())
    return events.codes()[0]


def _suwayomi_unreachable(tmp: Path) -> str:
    from shelfhold.sdk.source import TemporarilyUnavailable
    from tests.follow_env import follow_bench

    with follow_bench() as bench:

        async def down(fetcher: Any) -> Any:
            raise TemporarilyUnavailable("connection refused")

        bench.service.followed_series = down  # type: ignore[method-assign]
        for _ in range(3):
            bench.run()
        return str(bench.events("source.broken") and "source.broken")


def _disk_low(tmp: Path) -> str:
    from shelfhold.infrastructure.observability.disk_guard import DiskGuard, DiskLowError

    volume = FaultSimulator().volume(100, 1)
    guard = DiskGuard({"store": tmp}, usage=volume)
    guard.evaluate()
    with pytest.raises(DiskLowError) as raised:
        guard.require_space()
    return raised.value.code


def _second_instance(tmp: Path) -> str:
    from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError

    first = InstanceLock(tmp)
    first.acquire()
    try:
        with pytest.raises(InstanceLockHeldError) as raised:
            InstanceLock(tmp).acquire()
    finally:
        first.release()
    return raised.value.code


def _series_dir_not_writable(tmp: Path) -> str:
    made = make_env(tmp)

    def refused(*_a: object, **_k: object) -> None:
        raise PermissionError(13, "Permission denied")

    made.fsops.write_atomic = refused  # type: ignore[method-assign]
    return probe_folder(made.fsops, str(made.manga)).event or ""


def _mount_missing(tmp: Path) -> str:
    from shelfhold.export.domain.physical_roots import RootState

    made = make_env(tmp)
    (made.library / ".shelfhold-mount-id").unlink()  # the volume came back empty
    ok = made.fsops.status(str(made.manga)).state is RootState.OK
    return "" if ok else "mount.sentinel_missing"


SCENARIOS = {
    "kavita_unreachable": _kavita_unreachable,
    "suwayomi_unreachable": _suwayomi_unreachable,
    "disk_low": _disk_low,
    "second_instance": _second_instance,
    "series_dir_not_writable": _series_dir_not_writable,
    "mount_missing": _mount_missing,
}


@pytest.mark.req("ENF-OPS-011")
def test_every_simulable_row_replays_and_every_other_row_names_its_proof(tmp_path: Path) -> None:
    assert {r.key for r in FAILURE_MATRIX if r.simulated} == set(SCENARIOS)
    for row in FAILURE_MATRIX:
        assert row.simulated or row.proof, f"{row.key}: neither replayed nor proved elsewhere"
    for number, (key, scenario) in enumerate(sorted(SCENARIOS.items())):
        row = next(r for r in FAILURE_MATRIX if r.key == key)
        folder = tmp_path / f"run{number}"
        folder.mkdir()
        assert scenario(folder) == row.event, key


@pytest.mark.req("ENF-OPS-011")
def test_the_matrix_carries_the_rows_the_review_found_missing() -> None:
    keys = {r.key: r for r in FAILURE_MATRIX}
    assert {"lncrawl_unreachable", "store_missing_after_restore", "kavita_asleep_sablier"} <= set(
        keys
    )
    assert keys["suwayomi_schema_changed"].event == "source.version_unsupported"


BACKEND = Path(__file__).resolve().parents[2]


def _test_source(nodeid: str) -> str | None:
    """The source of the test function a pytest node id names, or None if there is none."""
    path, _, name = nodeid.partition("::")
    file = BACKEND / path
    if not file.is_file() or not name:
        return None
    text = file.read_text("utf-8")
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(text, node)
    return None


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_proof_nodeids_exist_and_event_matches() -> None:
    """A row that is not replayed here names the pytest test that proves it: that test exists, and
    it compares with the event of the row (it names it), not with a code it made up."""
    for row in FAILURE_MATRIX:
        if row.simulated:
            continue
        if row.proof.startswith("lot "):
            assert row.milestone != "M1a", f"{row.key}: a row of M1a is proved by a real test"
            continue  # an M1b row whose emitter ships with its lot
        source = _test_source(row.proof)
        assert source is not None, f"{row.key}: {row.proof!r} is not a test that exists"
        assert f'"{row.event}"' in source, f"{row.key}: {row.proof} never names {row.event}"


@pytest.mark.req("EF-SRC-036", "ENF-OPS-011")
def test_suwayomi_reinstalled_row_is_closed_by_its_real_test() -> None:
    """M1b L18: the row no longer waits for a lot, it names the test that plays it."""
    row = next(r for r in FAILURE_MATRIX if r.key == "suwayomi_reinstalled")
    assert row.proof == (
        "tests/integration/test_suwayomi_reinstall.py"
        "::test_connected_service_reinstall_pauses_without_cascade"
    )
    assert (row.event, row.exit_command, row.behaviour) == (
        "source.instance_changed",
        "source reconnect",
        "pause_service",
    )


@pytest.mark.req("EF-INT-022", "ENF-OPS-011")
def test_sablier_rows_are_closed_by_their_real_tests() -> None:
    """M1b L21: `sablier_slow` and `kavita_asleep_sablier` name the test that plays them."""
    slow = next(r for r in FAILURE_MATRIX if r.key == "sablier_slow")
    asleep = next(r for r in FAILURE_MATRIX if r.key == "kavita_asleep_sablier")
    assert slow.proof == (
        "tests/integration/test_wake_follow.py::test_wake_deferred_event_after_3_batches"
    )
    assert asleep.proof == (
        "tests/integration/test_wake_kavita.py"
        "::test_kavita_asleep_wake_timeout_defers_without_unreachable_or_write"
    )
    for row in (slow, asleep):
        assert (row.event, row.behaviour, row.exit_command) == (
            "wake.deferred",
            "notify_only",
            None,
        )


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_suwayomi_health_rows_are_closed_by_their_real_tests() -> None:
    """M1b L22: `suwayomi_mute`, `suwayomi_downloads_dropped` and `suwayomi_stale_series` name the
    test that plays them."""
    health = "tests/integration/test_health_requeue.py"
    expected = {
        "suwayomi_mute": ("source.not_loaded", "test_semantic_health_detects_unloaded_source"),
        "suwayomi_downloads_dropped": (
            "source.downloads_dropped",
            "test_dropped_chapters_requeued_with_cap",
        ),
        "suwayomi_stale_series": ("series.stale", "test_series_stale_aggregated"),
    }
    for key, (event, name) in expected.items():
        row = next(r for r in FAILURE_MATRIX if r.key == key)
        assert row.proof == f"{health}::{name}"
        assert row.event == event and row.behaviour in {"degrade", "notify_only"}


@pytest.mark.req("ENF-OPS-011")
def test_failure_matrix_m2_rows_labelled_m2() -> None:
    """M2 L00 (D18 of M1b): the metadata and lncrawl rows wait for their M2 emitters (L13, L25)."""
    assert {r.key for r in rows_for("M2")} == {"metadata_api_down", "lncrawl_unreachable"}
    for row in rows_for("M2"):
        assert row.proof == "lot M2: the emitter ships with it", row.key
        assert not row.simulated
    assert "metadata_api_down" not in {r.key for r in rows_for("M1b")}
    assert "lncrawl_unreachable" not in {r.key for r in rows_for("M1b")}
