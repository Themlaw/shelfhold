"""ENF-OPS-011: the failure matrix (spec section 7 bis) replayed deterministically.

Each row has its event (EF-INT-006), the error code of the same identifier (EF-UX-003) and, when a
human has to act, an exit command (EF-INT-023)."""

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
