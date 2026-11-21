"""M6 L05 (EF-USR-024 pass 13, EF-SRC-016, ENF-QUAL-003): the kit, in isolated mode, on the plugins
the repository ships (the source, the metadata provider and the connected service that have a
conformance test). The sandbox is described by an observation on names that do not exist, as the
companion's subprocess would find them; the real one is observed by the compose of the e2e stack
(`e2e/tests/test_m6_plugin_runner.py`)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from shelfhold_sdk.fetcher import Response
from shelfhold_sdk.metadata import EntityRef, MatchHints
from shelfhold_sdk.testing import (
    FakeFetcher,
    SandboxView,
    observe_sandbox,
    run_metadata_conformance,
    run_source_conformance,
)

from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.native.gutenberg import GutenbergSource
from shelfhold.plugin_host.adapters.native.openlibrary import OpenLibraryProvider
from tests.fakes.openlibrary.server import API_HOST, ISBN, record
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.gutenberg_env import CATALOG_URL, CSV, MIRROR_URL

ISOLATION = {
    "isolation_no_data",
    "isolation_no_cli_socket",
    "isolation_env_closed",
    "isolation_uid_distinct",
}


def sandbox(root: Path) -> SandboxView:
    """What a plugin's subprocess in the companion finds: none of the four paths, a closed
    environment (the test's own is not looked at)."""
    gone = tuple(str(root / n) for n in ("data", "store", "library", "data/shelfhold.sock"))
    return observe_sandbox(paths=gone, environ={"PATH": "/usr/bin", "LANG": "C"})


@pytest.mark.req("EF-USR-024", "EF-SRC-016", "ENF-QUAL-003")
def test_first_party_source_passes_the_kit_in_isolated_mode(tmp_path: Path) -> None:
    settings = {"catalog_url": CATALOG_URL, "mirror_url": MIRROR_URL}
    responses = {("GET", CATALOG_URL): Response(status=200, body=CSV.encode())}
    report = asyncio.run(
        run_source_conformance(
            GutenbergSource(),
            FakeFetcher(responses, settings=settings),
            reinstall=GutenbergSource,
            isolated=True,
            sandbox=sandbox(tmp_path),
            core_uid=_other_uid(),
        )
    )
    assert report.ok, report.failures
    assert ISOLATION <= {c.name for c in report.checks}


@pytest.mark.req("EF-USR-024", "EF-SRC-016", "ENF-QUAL-003")
def test_first_party_metadata_provider_passes_the_kit_in_isolated_mode(tmp_path: Path) -> None:
    books = f"https://{API_HOST}/api/books?bibkeys=ISBN:{ISBN}&format=json&jscmd=data"
    body = json.dumps({f"ISBN:{ISBN}": record()}).encode()
    report = asyncio.run(
        run_metadata_conformance(
            OpenLibraryProvider(),
            FakeFetcher({("GET", books): Response(status=200, body=body)}),
            targets=[EntityRef(entity_type="edition", ids={"isbn13": ISBN})],
            hints=MatchHints(title="Synthetic title"),
            isolated=True,
            sandbox=sandbox(tmp_path),
            core_uid=_other_uid(),
        )
    )
    report.assert_ok()
    assert ISOLATION <= {c.name for c in report.checks}


@pytest.mark.req("EF-USR-024", "EF-SRC-016", "ENF-QUAL-003")
def test_first_party_connected_service_passes_the_kit_in_isolated_mode(tmp_path: Path) -> None:
    server = FakeSuwayomi(auth_mode="none")
    fetcher = FakeSuwayomiFetcher(server)

    def reinstall() -> Any:
        server.reinstall()
        return SuwayomiService(BASE_URL)

    report = asyncio.run(
        run_source_conformance(
            SuwayomiService(BASE_URL),
            fetcher,
            reinstall=reinstall,
            isolated=True,
            sandbox=sandbox(tmp_path),
            core_uid=_other_uid(),
        )
    )
    assert report.ok, report.failures
    assert ISOLATION <= {c.name for c in report.checks}


@pytest.mark.req("EF-USR-024", "EF-SRC-016", "ENF-QUAL-003")
def test_the_kit_in_isolated_mode_fails_a_plugin_that_sees_the_data(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    report = asyncio.run(
        run_source_conformance(
            GutenbergSource(),
            FakeFetcher(
                {("GET", CATALOG_URL): Response(status=200, body=CSV.encode())},
                settings={"catalog_url": CATALOG_URL, "mirror_url": MIRROR_URL},
            ),
            reinstall=GutenbergSource,
            isolated=True,
            sandbox=sandbox(tmp_path),
        )
    )
    assert {c.name for c in report.failures} == {"isolation_no_data"}


def _other_uid() -> int:
    import os

    return os.getuid() + 1
