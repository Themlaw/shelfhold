"""M6 L05 (EF-USR-024 pass 13, EF-SRC-016, ENF-QUAL-003): the kit's isolation check.

Played in isolated mode, the kit asks the process it runs in what it can see: no `/data`, no
`/store`, no `/library`, no CLI socket, no `SHELFHOLD_*` variable, and a uid that is not the
core's. In a plugin's own subprocess (the companion's) it is what the plugin would find; the tests
describe the sandbox with paths a test makes up.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

import pytest
from shelfhold_sdk.metadata import MatchHints
from shelfhold_sdk.testing import (
    FakeMetadataProvider,
    FakeRangedConnectedService,
    SandboxView,
    assert_conforms,
    fake_metadata_fetcher,
    fake_ranged_fetcher,
    observe_sandbox,
    run_isolation_check,
    run_metadata_conformance,
    run_source_conformance,
)
from shelfhold_sdk.testing.destination_kit import (
    DestinationScenario,
    run_destination_conformance,
)
from shelfhold_sdk.testing.isolation import SANDBOX_PATHS

ISOLATION = {
    "isolation_no_data",
    "isolation_no_cli_socket",
    "isolation_env_closed",
    "isolation_uid_distinct",
}


CLOSED = {"PATH": "/usr/bin"}
CORE = 1000  # the core's uid, for the views a test makes up (theirs is 1001)
OTHER = os.getuid() + 1  # a uid that is not this process's, for the observed ones


def failed(report: object) -> set[str]:
    return {c.name for c in report.failures}  # type: ignore[attr-defined]


def view(**over: object) -> SandboxView:
    base: dict[str, object] = {"uid": 1001, "visible": (), "env_names": ("PATH", "LANG")}
    return SandboxView(**(base | over))  # type: ignore[arg-type]


@pytest.mark.req("EF-USR-024", "EF-SRC-016", "ENF-QUAL-003")
def test_plugin_subprocess_never_sees_data_socket(tmp_path: Path) -> None:
    """The check of the kit (pass 13): the subprocess sees neither `/data` nor the socket."""
    assert set(SANDBOX_PATHS) == {"/data", "/store", "/library", "/data/shelfhold.sock"}
    # what a clean sandbox shows: the paths are absent, nothing of Shelfhold's in the environment
    clean = run_isolation_check(view(), core_uid=CORE)
    assert clean.ok and {c.name for c in clean.checks} == ISOLATION, clean.failures
    # observed for real, on names a test makes up, absent
    gone = tuple(str(tmp_path / n) for n in ("data", "store", "library", "data/shelfhold.sock"))
    seen = observe_sandbox(paths=gone, environ=CLOSED)
    assert seen.visible == () and seen.uid == os.getuid()
    # `/data`, `/store` or `/library` there, readable: the check fails and names which
    for name in ("data", "store", "library"):
        (tmp_path / name).mkdir()
        looked = observe_sandbox(paths=gone, environ=CLOSED)
        assert str(tmp_path / name) in looked.visible
        report = run_isolation_check(looked, core_uid=OTHER)
        assert failed(report) == {"isolation_no_data"}, report.checks
        assert name in next(c.detail for c in report.failures)
        (tmp_path / name).rmdir()
    # the CLI socket there: a real unix socket, where the check looks for it
    (tmp_path / "data").mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(tmp_path / "data/shelfhold.sock"))
    listener.listen()
    try:
        looked = observe_sandbox(paths=gone, environ=CLOSED)
        assert failed(run_isolation_check(looked, core_uid=OTHER)) >= {"isolation_no_cli_socket"}
    finally:
        listener.close()


@pytest.mark.req("EF-USR-024", "ENF-SEC-009")
def test_isolation_check_uid_and_environment() -> None:
    same = run_isolation_check(view(uid=CORE), core_uid=CORE)
    assert failed(same) == {"isolation_uid_distinct"}
    assert run_isolation_check(view(uid=0), core_uid=CORE).failures  # root is nobody's sandbox
    # without the core's uid the check says so (it cannot compare), it does not guess
    unknown = run_isolation_check(view(), core_uid=None)
    assert unknown.ok and "isolation_uid_distinct" not in {c.name for c in unknown.checks}
    leaky = run_isolation_check(view(env_names=("PATH", "SHELFHOLD_SETUP_TOKEN")), core_uid=CORE)
    assert failed(leaky) == {"isolation_env_closed"}
    assert "SHELFHOLD_SETUP_TOKEN" in next(c.detail for c in leaky.failures)
    assert "isolation_env_closed" in failed(
        run_isolation_check(view(env_names=("DB_PASSWORD",)), core_uid=CORE)
    )


@pytest.mark.req("EF-USR-024", "ENF-QUAL-003")
def test_the_kit_plays_the_isolation_check_in_isolated_mode_only() -> None:
    service = FakeRangedConnectedService()

    async def run(**kw: object) -> object:
        return await run_source_conformance(
            service,
            fake_ranged_fetcher(),
            reinstall=service.reinstall,
            **kw,  # type: ignore[arg-type]
        )

    plain = asyncio.run(run())
    assert ISOLATION.isdisjoint({c.name for c in plain.checks})  # type: ignore[attr-defined]
    clean = asyncio.run(run(isolated=True, sandbox=view(), core_uid=CORE))
    assert {c.name for c in clean.checks} >= ISOLATION  # type: ignore[attr-defined]
    assert clean.ok, clean.failures  # type: ignore[attr-defined]
    seen = asyncio.run(run(isolated=True, sandbox=view(visible=("/data",)), core_uid=CORE))
    assert failed(seen) == {"isolation_no_data"}
    # and the plugin author's own entry point raises on it
    with pytest.raises(AssertionError, match="isolation_no_data"):
        assert_conforms(
            service,
            fake_ranged_fetcher(),
            reinstall=service.reinstall,
            isolated=True,
            sandbox=view(visible=("/data",)),
        )
    # a sandbox is never taken for granted: no view given, the process is observed
    observed = asyncio.run(run(isolated=True))
    assert ISOLATION - {"isolation_uid_distinct"} <= {c.name for c in observed.checks}  # type: ignore[attr-defined]


@pytest.mark.req("EF-USR-024", "ENF-QUAL-003")
def test_the_isolation_check_is_played_by_every_chapter_of_the_kit() -> None:
    bad = view(visible=("/store",))
    metadata = asyncio.run(
        run_metadata_conformance(
            FakeMetadataProvider(),
            fake_metadata_fetcher(),
            targets=[],
            hints=MatchHints(title="Synthetic title"),
            isolated=True,
            sandbox=bad,
        )
    )
    assert "isolation_no_data" in failed(metadata)
    scenario = DestinationScenario(key="demo")
    destination = run_destination_conformance(object(), scenario, isolated=True, sandbox=bad)
    assert "isolation_no_data" in {c.name for c in destination.checks}


@pytest.mark.req("EF-USR-024", "ENF-QUAL-003")
def test_the_pytest_tool_runs_the_isolation_check_on_request() -> None:
    from shelfhold_sdk.testing.pytest_plugin import check_conformance

    service = FakeRangedConnectedService()
    run = check_conformance.__wrapped__()  # type: ignore[attr-defined]
    report = run(
        service, fake_ranged_fetcher(), reinstall=service.reinstall, isolated=True, sandbox=view()
    )
    assert ISOLATION - {"isolation_uid_distinct"} <= {c.name for c in report.checks}


@pytest.mark.req("EF-USR-024", "ENF-SEC-009")
def test_sandbox_view_reads_as_json_for_the_companion() -> None:
    """`python -m shelfhold_sdk.testing.isolation` prints what the process sees as JSON, so that
    the companion can play the check in a plugin's subprocess and read the answer."""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-I", "-m", "shelfhold_sdk.testing.isolation"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"]},
    )
    data = json.loads(done.stdout)
    assert data["uid"] == os.getuid()
    assert set(data) == {"uid", "visible", "env_names"}
