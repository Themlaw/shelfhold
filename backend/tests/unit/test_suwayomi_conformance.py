"""ENF-QUAL-003, EF-SRC-036, EF-SRC-033: the conformance kit run on the Suwayomi adapter."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import Capability, SourceManifest
from shelfhold.sdk.testing.kit import ConformanceReport, run_source_conformance
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher

MANIFEST = Path(__file__).resolve().parents[3] / "sources" / "meta_suwayomi" / "manifest.json"


def conformance(
    make: Any = None,
) -> tuple[ConformanceReport, FakeSuwayomi, FakeSuwayomiFetcher]:
    server = FakeSuwayomi(auth_mode="none")
    fetcher = FakeSuwayomiFetcher(server)
    build = make or (lambda: SuwayomiService(BASE_URL))
    service = build()

    def reinstall() -> Any:
        server.reinstall()  # base recreated, local ids renumbered
        return build()

    report = asyncio.run(run_source_conformance(service, fetcher, reinstall=reinstall))
    return report, server, fetcher


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-033")
def test_meta_suwayomi_passes_conformance_kit() -> None:
    report, server, fetcher = conformance()
    assert report.ok, report.failures
    names = {c.name for c in report.checks}
    assert {"manifest", "capabilities_implemented", "unit_identity", "json_roundtrip",
            "fetch_unit", "server_queue", "health_probe", "simulated_reinstall",
            "offline"} <= names  # fmt: skip
    assert fetcher.unscripted == [] and server.violations == []


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-036")
def test_conformance_kit_simulated_reinstall_no_new_variant() -> None:
    report, _, _ = conformance()
    reinstall = next(c for c in report.checks if c.name == "simulated_reinstall")
    assert reinstall.ok, reinstall.detail  # same upstream identities, hence no new Variant

    class LeaksLocalId(SuwayomiService):
        """A faulty service that hands out the local database id as the unit identity."""

        async def list_units(self, f: Any, ref: SeriesRef) -> list[UnitInfo]:
            units = await super().list_units(f, ref)
            return [
                u.model_copy(update={"remote_unit_id": f"local:{u.memo['fetch_handle']}"})
                for u in units
            ]

    bad, _, _ = conformance(lambda: LeaksLocalId(BASE_URL))
    failed = {c.name for c in bad.failures}
    assert "simulated_reinstall" in failed
    assert not bad.ok


@pytest.mark.req("EF-SRC-033", "EF-SRC-008")
def test_meta_suwayomi_manifest_and_declared_capabilities() -> None:
    service = SuwayomiService(BASE_URL)
    on_disk = SourceManifest.model_validate_json(MANIFEST.read_text("utf-8"))
    assert service.manifest == on_disk  # the manifest of sources/meta_suwayomi is the one served
    assert service.manifest.key == "suwayomi"
    assert Capability.SUB_SOURCES in service.manifest.capabilities
    assert Capability.RANGED_CONTENT not in service.manifest.capabilities  # one CBZ per chapter
    assert on_disk.content_trust == "untrusted"
    # declared by the admin per instance, never baked into the manifest
    assert json.loads(MANIFEST.read_text("utf-8"))["permissions"]["lan_targets"] == []
    declared = service.connected
    assert ConnectedCapability.SERVER_QUEUE in declared
    assert ConnectedCapability.HEALTH_PROBE in declared
    assert ConnectedCapability.AUTH_MODE in declared
    assert ConnectedCapability.INSTANCE_FINGERPRINT not in declared  # M1b
