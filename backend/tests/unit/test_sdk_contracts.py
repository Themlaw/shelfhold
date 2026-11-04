"""Source, ranged_content and ConnectedService contracts, seen through the fake plugin."""

import asyncio
from decimal import Decimal

import pytest

from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import FileAsset, RangedPayload, RangeRef, UnitRef
from shelfhold.sdk.source import Capability, ContentRemoved, SourceError, TemporarilyUnavailable
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService, fake_ranged_fetcher


async def _units(service: FakeRangedConnectedService) -> list:  # type: ignore[type-arg]
    fetcher = fake_ranged_fetcher()
    series = (await service.search(fetcher, "", {}, None)).items[0]
    return [series, *await service.list_units(fetcher, series)]


@pytest.mark.req("EF-SRC-032")
def test_ranged_content_is_an_optional_capability_with_volume_flags() -> None:
    service = FakeRangedConnectedService()
    assert Capability.RANGED_CONTENT in service.manifest.capabilities
    series, *units = asyncio.run(_units(service))
    assert units[0].volume_label == "Vol.1" and units[0].volume_closed is True
    assert units[8].volume_label == "Vol.2" and units[8].volume_closed is False
    assert units[0].number_sort == Decimal(1)
    assert series.source_key == service.manifest.key


@pytest.mark.req("EF-SRC-032")
def test_fetch_range_one_file_covers_eight_units_and_closes_the_volume() -> None:
    service = FakeRangedConnectedService()
    fetcher = fake_ranged_fetcher()

    async def run() -> RangedPayload:
        series, *units = await _units(service)
        target = RangeRef(series=series, first=units[0], last=units[7], volume_label="Vol.1")
        return await service.fetch_range(fetcher, target)

    payload = asyncio.run(run())
    assert isinstance(payload.asset, FileAsset)
    assert payload.covers == [f"fake-ranged:ch-{n}" for n in range(1, 9)]  # ordered
    assert payload.volume_closed is True
    assert set(payload.bounds) <= set(payload.covers)


@pytest.mark.req("EF-SRC-032")
def test_rolling_volume_is_not_closed() -> None:
    service = FakeRangedConnectedService()
    fetcher = fake_ranged_fetcher()

    async def run() -> RangedPayload:
        series, *units = await _units(service)
        return await service.fetch_range(fetcher, RangeRef(series=series, first=units[8], last=units[9]))

    payload = asyncio.run(run())
    assert payload.covers == ["fake-ranged:ch-9", "fake-ranged:ch-10"]
    assert payload.volume_closed is False


@pytest.mark.req("EF-SRC-033")
def test_connected_capabilities_are_optional_and_declared() -> None:
    service = FakeRangedConnectedService()
    declared = service.connected
    assert ConnectedCapability.SERVER_QUEUE in declared
    # minimal service: no categories, no release_claim (ADR-0041)
    assert ConnectedCapability.CATEGORIES not in declared
    assert ConnectedCapability.RELEASE_CLAIM not in declared
    assert not hasattr(service, "followed_series")
    assert not hasattr(service, "release")
    for name in ("CATEGORIES", "SERVER_QUEUE", "RELEASE_CLAIM", "UPSTREAM_RESTORE",
                 "HEALTH_PROBE", "AUTH_MODE", "WAKE", "INSTANCE_FINGERPRINT"):  # fmt: skip
        assert hasattr(ConnectedCapability, name)


@pytest.mark.req("EF-SRC-033", "EF-SRC-034")
def test_server_queue_target_is_a_unit_or_a_range() -> None:
    service = FakeRangedConnectedService()
    fetcher = fake_ranged_fetcher()

    async def run() -> tuple[RangedPayload, object, list[str]]:
        series, *units = await _units(service)
        range_ticket = await service.request(fetcher, RangeRef(series=series, first=units[0], last=units[7]))
        unit_ticket = await service.request(fetcher, UnitRef(series=series, unit=units[0]))
        states = []
        for _ in range(3):
            states.append(await service.poll(fetcher, range_ticket))
        ranged = await service.fetch(fetcher, range_ticket)
        single = await service.fetch(fetcher, unit_ticket)
        await service.cancel(fetcher, unit_ticket)
        assert range_ticket.token != unit_ticket.token  # one ticket per target
        assert range_ticket.target_kind == "range" and unit_ticket.target_kind == "unit"
        return ranged, single, states  # type: ignore[return-value]

    ranged, single, states = asyncio.run(run())
    assert isinstance(ranged, RangedPayload) and len(ranged.covers) == 8  # one file, 8 units
    assert not isinstance(single, RangedPayload)
    assert states[-1] == "ready"


@pytest.mark.req("EF-SRC-034")
def test_fake_declares_ranged_content_and_server_queue_together() -> None:
    service = FakeRangedConnectedService()
    assert Capability.RANGED_CONTENT in service.manifest.capabilities
    assert ConnectedCapability.SERVER_QUEUE in service.connected


@pytest.mark.req("EF-SRC-001")
def test_typed_errors_are_source_errors() -> None:
    assert issubclass(ContentRemoved, SourceError)
    assert issubclass(TemporarilyUnavailable, SourceError)
