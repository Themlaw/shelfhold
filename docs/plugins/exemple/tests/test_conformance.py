"""Le plugin d'exemple passe le kit de conformite, en processus et isole, sans l'hote."""

import asyncio
from pathlib import Path

import pytest
from plugin_exemple.source import ExampleSource
from shelfhold_sdk.testing import FakeFetcher, observe_sandbox, run_source_conformance

CASSETTE = Path(__file__).parent / "cassettes" / "catalogue.json"


def test_the_example_source_conforms_in_process() -> None:
    fetcher = FakeFetcher.from_cassette(CASSETTE)
    report = asyncio.run(
        run_source_conformance(
            ExampleSource(),
            fetcher,
            reinstall=lambda: ExampleSource(),
            isolated=False,
        )
    )
    assert report.ok, report.failures
    assert fetcher.unscripted == []


def test_the_example_source_conforms_isolated(tmp_path: Path) -> None:
    fetcher = FakeFetcher.from_cassette(CASSETTE)
    gone = tuple(str(tmp_path / n) for n in ("data", "store", "library", "data/shelfhold.sock"))
    sandbox = observe_sandbox(paths=gone, environ={"PATH": "/usr/bin", "LANG": "C"})
    report = asyncio.run(
        run_source_conformance(
            ExampleSource(),
            fetcher,
            reinstall=lambda: ExampleSource(),
            isolated=True,
            sandbox=sandbox,
            core_uid=999999,
        )
    )
    assert report.ok, report.failures


@pytest.mark.req("EF-SRC-020")
def test_example_plugin_declares_namespaced_event_and_waiting_exit() -> None:
    from shelfhold_sdk.events import plugin_event_type

    manifest = ExampleSource().manifest
    assert manifest.waiting_states
    state = manifest.waiting_states[0]
    assert state.exit in ("retry", "recheck", "reconfigure")
    event_type = plugin_event_type(manifest.key, state.name)
    assert event_type.startswith(f"plugin.{manifest.key}.")