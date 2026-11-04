"""Conformance kit (ENF-QUAL-003): runs offline on synthetic fixtures, record_mode none."""

import asyncio
import json
from pathlib import Path

import pytest

from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.testing.fakes import (
    FakeFetcher,
    FakeRangedConnectedService,
    NetworkForbidden,
    fake_ranged_fetcher,
)
from shelfhold.sdk.testing.kit import ConformanceReport, run_source_conformance


def _run(service: FakeRangedConnectedService, fetcher: FakeFetcher | None = None) -> ConformanceReport:
    return asyncio.run(
        run_source_conformance(
            service, fetcher or fake_ranged_fetcher(), reinstall=lambda: service.reinstall()
        )
    )


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-032", "EF-SRC-034")
def test_conformance_kit_runs_offline_on_fake_ranged_plugin() -> None:
    fetcher = fake_ranged_fetcher()
    report = _run(FakeRangedConnectedService(), fetcher)
    assert report.ok, report.failures
    names = {c.name for c in report.checks}
    assert {"manifest", "api_compatible", "capabilities_implemented", "references_opaque",
            "unit_identity", "json_roundtrip", "ranged_content", "server_queue",
            "simulated_reinstall", "offline"} <= names  # fmt: skip
    assert fetcher.record_mode == "none"
    assert fetcher.unscripted == []  # nothing tried to leave the process


@pytest.mark.req("ENF-QUAL-003")
def test_fetcher_refuses_any_unscripted_request() -> None:
    fetcher = FakeFetcher()
    with pytest.raises(NetworkForbidden):
        asyncio.run(fetcher.get("https://unscripted.invalid/"))
    assert fetcher.unscripted == ["GET https://unscripted.invalid/"]


@pytest.mark.req("ENF-QUAL-003")
def test_kit_flags_network_access_outside_the_script() -> None:
    class Leaky(FakeRangedConnectedService):
        async def list_units(self, f, ref):  # type: ignore[no-untyped-def]
            await f.get("https://real-site.invalid/chapters")
            return await super().list_units(f, ref)

    report = _run(Leaky())
    assert not report.ok
    assert any(c.name == "offline" and not c.ok for c in report.checks)


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-036")
def test_kit_simulated_reinstall_detects_unstable_identities() -> None:
    class Renumbering(FakeRangedConnectedService):
        async def list_units(self, f, ref):  # type: ignore[no-untyped-def]
            units = await super().list_units(f, ref)
            return [
                u.model_copy(update={"remote_unit_id": f"local-{self.handle_offset}-{i}"})
                for i, u in enumerate(units)
            ]

        def reinstall(self) -> "Renumbering":
            return Renumbering(handle_offset=self.handle_offset + 1000)

    service = Renumbering()
    report = asyncio.run(
        run_source_conformance(service, fake_ranged_fetcher(), reinstall=service.reinstall)
    )
    failed = [c.name for c in report.failures]
    assert "simulated_reinstall" in failed


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-036")
def test_kit_requires_a_reinstall_factory_for_a_connected_service() -> None:
    report = asyncio.run(run_source_conformance(FakeRangedConnectedService(), fake_ranged_fetcher()))
    assert [c.name for c in report.failures] == ["simulated_reinstall"]


@pytest.mark.req("ENF-QUAL-003")
def test_kit_flags_duplicate_unit_identity_and_secret_in_reference() -> None:
    class Bad(FakeRangedConnectedService):
        async def search(self, f, q, filters, cursor, *, sub=None):  # type: ignore[no-untyped-def]
            page = await super().search(f, q, filters, cursor, sub=sub)
            page.items[0] = SeriesRef(
                source_key=self.manifest.key,
                remote_id="https://user:hunter2@host.invalid/s/1",
                title="x",
            )
            return page

        async def list_units(self, f, ref):  # type: ignore[no-untyped-def]
            units = await super().list_units(f, ref)
            units[1] = units[1].model_copy(update={"remote_unit_id": units[0].remote_unit_id})
            return units

    report = _run(Bad())
    failed = {c.name for c in report.failures}
    assert {"references_opaque", "unit_identity"} <= failed


@pytest.mark.req("ENF-QUAL-003")
def test_kit_flags_declared_capability_without_implementation() -> None:
    service = FakeRangedConnectedService()
    service.connected |= ConnectedCapability.CATEGORIES  # followed_series/set_follow are missing
    report = _run(service)
    failed = {c.name for c in report.failures}
    assert "capabilities_implemented" in failed


@pytest.mark.req("ENF-QUAL-003")
def test_fetcher_replays_a_synthetic_cassette(tmp_path: Path) -> None:
    cassette = tmp_path / "c.json"
    cassette.write_text(
        json.dumps(
            {
                "interactions": [
                    {"method": "GET", "url": "https://a.invalid/x", "status": 200, "body": "ok"}
                ]
            }
        ),
        encoding="utf-8",
    )
    fetcher = FakeFetcher.from_cassette(cassette)
    response = asyncio.run(fetcher.get("https://a.invalid/x"))
    assert (response.status, response.body) == (200, b"ok")
    with pytest.raises(NetworkForbidden):
        asyncio.run(fetcher.get("https://a.invalid/other"))


def test_unit_info_import_is_used() -> None:  # keeps the kit's public model importable
    assert UnitInfo.model_fields["remote_unit_id"].is_required()
