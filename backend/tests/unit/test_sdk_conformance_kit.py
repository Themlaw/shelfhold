"""Conformance kit (ENF-QUAL-003): runs offline on synthetic fixtures, record_mode none."""

import asyncio
import json
from pathlib import Path

import pytest

from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.testing.fakes import (
    FakeFetcher,
    FakeRangedConnectedService,
    NetworkForbidden,
    fake_ranged_fetcher,
)
from shelfhold.sdk.testing.kit import ConformanceReport, run_source_conformance


def _run(
    service: FakeRangedConnectedService, fetcher: FakeFetcher | None = None
) -> ConformanceReport:
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
    report = asyncio.run(
        run_source_conformance(FakeRangedConnectedService(), fake_ranged_fetcher())
    )
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


@pytest.mark.req("ENF-QUAL-003")
def test_assert_conforms_and_pytest_tool_for_plugin_authors() -> None:
    from shelfhold.sdk.testing import assert_conforms
    from shelfhold.sdk.testing.pytest_plugin import check_conformance, offline_fetcher

    service = FakeRangedConnectedService()
    report = assert_conforms(service, fake_ranged_fetcher(), reinstall=service.reinstall)
    assert report.ok
    with pytest.raises(AssertionError, match="simulated_reinstall"):
        assert_conforms(service, fake_ranged_fetcher())  # no reinstall factory
    # the fixtures wrap the same functions
    assert offline_fetcher.__wrapped__().record_mode == "none"  # type: ignore[attr-defined]
    run = check_conformance.__wrapped__()  # type: ignore[attr-defined]
    assert run(service, fake_ranged_fetcher(), reinstall=service.reinstall).ok


class _Plain(FakeRangedConnectedService):
    """A simple Source: no connected capability at all."""

    def __init__(self, handle_offset: int = 0) -> None:
        super().__init__(handle_offset)
        self.connected = None  # type: ignore[assignment]


def _failed(report: ConformanceReport) -> set[str]:
    return {c.name for c in report.failures}


@pytest.mark.req("ENF-QUAL-003")
def test_kit_rejects_non_idempotent_list_units() -> None:
    class Drifting(FakeRangedConnectedService):
        calls = 0

        async def list_units(self, f, ref):  # type: ignore[no-untyped-def]
            units = await super().list_units(f, ref)
            type(self).calls += 1
            return units[: len(units) - (type(self).calls % 2)]

    assert "list_units_idempotent" in _failed(_run(Drifting()))


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-036")
def test_kit_reinstall_compares_source_key_and_needs_a_fresh_instance() -> None:
    class Rekeyed(FakeRangedConnectedService):
        async def search(self, f, q, filters, cursor, *, sub=None):  # type: ignore[no-untyped-def]
            page = await super().search(f, q, filters, cursor, sub=sub)
            key = f"fake-{self.handle_offset}"
            page.items = [i.model_copy(update={"source_key": key}) for i in page.items]
            return page

        def reinstall(self) -> "Rekeyed":
            return Rekeyed(handle_offset=self.handle_offset + 1)

    service = Rekeyed()
    report = asyncio.run(
        run_source_conformance(service, fake_ranged_fetcher(), reinstall=service.reinstall)
    )
    assert "simulated_reinstall" in _failed(report)

    same = FakeRangedConnectedService()
    report = asyncio.run(
        run_source_conformance(same, fake_ranged_fetcher(), reinstall=lambda: same)
    )
    assert any(
        c.name == "simulated_reinstall" and "new instance" in c.detail for c in report.failures
    )


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-036")
def test_kit_runs_the_reinstall_check_for_a_plain_source_too() -> None:
    plain = _Plain()
    fetcher = fake_ranged_fetcher()
    good = asyncio.run(run_source_conformance(plain, fetcher, reinstall=plain.reinstall))
    assert good.ok, good.failures
    # no factory and no tool to reinstall: nothing to demand of a plain source
    assert "simulated_reinstall" not in {
        c.name for c in asyncio.run(run_source_conformance(_Plain(), fetcher)).checks
    }

    class Unstable(_Plain):
        async def list_units(self, f, ref):  # type: ignore[no-untyped-def]
            units = await super().list_units(f, ref)
            prefix = self.handle_offset
            return [
                u.model_copy(update={"remote_unit_id": f"{prefix}-{u.source_order}"}) for u in units
            ]

    bad = Unstable()
    report = asyncio.run(run_source_conformance(bad, fetcher, reinstall=bad.reinstall))
    assert "simulated_reinstall" in _failed(report)


@pytest.mark.req("ENF-QUAL-003")
def test_kit_rejects_a_poll_that_changes_state() -> None:
    class Consuming(FakeRangedConnectedService):
        async def poll(self, f, t):  # type: ignore[no-untyped-def]
            state = await super().poll(f, t)
            return "gone" if state == "ready" and self._tickets[t.token][1] > 3 else state

    report = _run(Consuming())
    assert any(c.name == "server_queue" and "poll" in c.detail for c in report.failures)
