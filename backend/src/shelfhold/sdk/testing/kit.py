# SPDX-License-Identifier: MIT
"""Conformance kit for sources and connected services (ENF-QUAL-003, EF-SRC-036).

Runs offline against a scripted Fetcher. Each check yields a named result; a plugin conforms when
every check passes. For a connected service the kit also plays a simulated reinstall of the tool
and demands the same upstream identities, hence no new Variant (EF-SRC-036).
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel

from shelfhold.sdk.connected import ConnectedCapability, ConnectedService
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.models import API_VERSION
from shelfhold.sdk.refs import (
    RangedPayload,
    RangeRef,
    SeriesRef,
    UnitInfo,
    UnitRef,
)
from shelfhold.sdk.source import Capability, Source, SourceManifest
from shelfhold.sdk.testing.fakes import NetworkForbidden

_USERINFO = re.compile(r"://[^/\s@]*@")
_SECRET_PARAM = re.compile(r"[?&;](?:token|key|secret|password|passwd|apikey|api_key|sig)=", re.I)
_POLL_LIMIT = 10

_REQUIRED_BY_CAPABILITY: dict[Capability, tuple[str, ...]] = {
    Capability.SEARCH: ("search",),
    Capability.URL_RESOLVE: ("resolve_url",),
    Capability.LOGIN: ("login", "refresh"),
    Capability.FILTERS: ("filters",),
    Capability.SUB_SOURCES: ("sub_sources",),
    Capability.RANGED_CONTENT: ("fetch_range",),
}
_REQUIRED_BY_CONNECTED: dict[ConnectedCapability, tuple[str, ...]] = {
    ConnectedCapability.CATEGORIES: ("followed_series", "set_follow"),
    ConnectedCapability.SERVER_QUEUE: ("request", "poll", "fetch", "cancel"),
    ConnectedCapability.RELEASE_CLAIM: ("release",),
    ConnectedCapability.UPSTREAM_RESTORE: ("snapshot_upstream", "restore_upstream"),
    ConnectedCapability.HEALTH_PROBE: ("semantic_health",),
    ConnectedCapability.AUTH_MODE: ("auth_mode",),
    ConnectedCapability.INSTANCE_FINGERPRINT: ("instance_fingerprint",),
}


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ConformanceReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and not self.failures

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name, ok, detail))

    def assert_ok(self) -> None:
        if not self.ok:
            lines = [f"{c.name}: {c.detail}" for c in self.failures]
            raise AssertionError("conformance failures:\n" + "\n".join(lines))


@dataclass
class _Discovery:
    refs: list[SeriesRef]
    series: SeriesRef
    units: list[UnitInfo]
    models: list[BaseModel]


def _roundtrips(model: BaseModel) -> bool:
    return type(model).model_validate_json(model.model_dump_json()) == model


def _leaks_secret(value: str) -> bool:
    return bool(_USERINFO.search(value) or _SECRET_PARAM.search(value))


async def _discover(source: Source, f: Fetcher, series_ref: SeriesRef | None) -> _Discovery:
    models: list[BaseModel] = []
    refs: list[SeriesRef] = []
    if series_ref is None:
        page = await source.search(f, "", {}, None)
        models.append(page)
        refs = list(page.items)
        if not refs:
            raise AssertionError("the synthetic fixtures must yield at least one series")
        series_ref = refs[0]
    info = await source.get_series(f, series_ref)
    units = await source.list_units(f, series_ref)
    models += [info, *units]
    if series_ref not in refs:
        refs.append(series_ref)
    return _Discovery(refs=refs, series=series_ref, units=units, models=models)


def _missing_methods(source: Source) -> list[str]:
    missing = [n for n in ("get_series", "list_units") if not callable(getattr(source, n, None))]
    caps = source.manifest.capabilities
    for cap, names in _REQUIRED_BY_CAPABILITY.items():
        if cap in caps:
            missing += [n for n in names if not callable(getattr(source, n, None))]
    if Capability.RANGED_CONTENT not in caps and not callable(getattr(source, "fetch_unit", None)):
        missing.append("fetch_unit")
    connected = getattr(source, "connected", None)
    if isinstance(connected, ConnectedCapability):
        for ccap, cnames in _REQUIRED_BY_CONNECTED.items():
            if ccap in connected:
                missing += [n for n in cnames if not callable(getattr(source, n, None))]
    return missing


async def run_source_conformance(
    source: Source,
    fetcher: Fetcher,
    *,
    reinstall: Callable[[], Source] | None = None,
    series_ref: SeriesRef | None = None,
) -> ConformanceReport:
    """Run every check against `source`; `fetcher` must be scripted (no network is possible)."""
    report = ConformanceReport()

    async def guarded(name: str, step: Callable[[], Awaitable[None]]) -> None:
        try:
            await step()
        except NetworkForbidden as exc:
            report.add(name, False, f"network access outside the script: {exc}")
        except Exception as exc:  # any failure of the plugin is a finding
            report.add(name, False, f"{type(exc).__name__}: {exc}")

    # --- manifest and api ---------------------------------------------------------------------
    manifest = source.manifest
    valid = isinstance(manifest, SourceManifest) and bool(manifest.key) and _roundtrips(manifest)
    report.add("manifest", valid, "" if valid else "manifest is not a valid SourceManifest")
    api_ok = manifest.api in (API_VERSION, API_VERSION - 1)
    report.add("api_compatible", api_ok, "" if api_ok else f"api {manifest.api} is not N or N-1")
    missing = _missing_methods(source)
    report.add(
        "capabilities_implemented", not missing, f"missing: {', '.join(missing)}" if missing else ""
    )

    # --- discovery, identities, serialisation ---------------------------------------------------
    found: list[_Discovery] = []

    async def discovery() -> None:
        found.append(await _discover(source, fetcher, series_ref))

    await guarded("discovery", discovery)
    if found:
        d = found[0]
        leaks = [
            r.remote_id for r in d.refs if _leaks_secret(r.remote_id) or _leaks_secret(r.source_key)
        ]
        report.add("references_opaque", not leaks, f"secret in reference: {leaks}" if leaks else "")
        ids = [u.remote_unit_id for u in d.units]
        problems = []
        if not all(ids):
            problems.append("empty remote_unit_id")
        if len(set(ids)) != len(ids):
            problems.append("duplicate remote_unit_id")
        if len({u.source_order for u in d.units}) != len(d.units):
            problems.append("duplicate source_order")
        report.add("unit_identity", not problems, "; ".join(problems))
        bad = [type(m).__name__ for m in [*d.models, *d.refs] if not _roundtrips(m)]
        report.add("json_roundtrip", not bad, f"not serialisable: {bad}" if bad else "")

        async def idempotent() -> None:
            again = await source.list_units(fetcher, d.series)
            same = [u.model_dump_json() for u in again] == [u.model_dump_json() for u in d.units]
            report.add(
                "list_units_idempotent",
                same,
                "" if same else "two list_units calls on one instance returned different units",
            )

        await guarded("list_units_idempotent", idempotent)
        await _check_payloads(source, fetcher, d, report, guarded)
        await _check_reinstall(source, fetcher, d, reinstall, series_ref, report, guarded)

    unscripted: list[str] = getattr(fetcher, "unscripted", [])
    report.add(
        "offline", not unscripted, f"unscripted requests: {unscripted}" if unscripted else ""
    )
    return report


async def _check_payloads(
    source: Source,
    f: Fetcher,
    d: _Discovery,
    report: ConformanceReport,
    guarded: Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]],
) -> None:
    caps = source.manifest.capabilities
    connected = getattr(source, "connected", None)
    queue = (
        isinstance(connected, ConnectedCapability) and ConnectedCapability.SERVER_QUEUE in connected
    )
    units = d.units
    window = [u for u in units if u.source_order <= units[0].source_order + 7] if units else []

    def range_target() -> RangeRef:
        return RangeRef(series=d.series, first=window[0], last=window[-1])

    async def ranged() -> None:
        payload = await source.fetch_range(f, range_target())
        covered = [u.remote_unit_id for u in window]
        problems = []
        if not payload.covers or any(c not in covered for c in payload.covers):
            problems.append("covers must be a non-empty subset of the requested units")
        if payload.covers != [c for c in covered if c in payload.covers]:
            problems.append("covers must follow the source order")
        if any(k not in payload.covers for k in payload.bounds):
            problems.append("bounds name a unit that is not covered")
        if not payload.asset.mime or not _roundtrips(payload):
            problems.append("asset or payload invalid")
        report.add("ranged_content", not problems, "; ".join(problems))

    async def single() -> None:
        payload = await source.fetch_unit(f, d.series, units[0])
        report.add("fetch_unit", _roundtrips(payload), "payload does not round-trip")

    async def queued() -> None:
        service: Any = source
        problems = []
        targets: list[UnitRef | RangeRef] = [UnitRef(series=d.series, unit=units[0])]
        if Capability.RANGED_CONTENT in caps:
            targets.append(range_target())
        tickets = [await service.request(f, t) for t in targets]
        if len({t.token for t in tickets}) != len(tickets):
            problems.append("one ticket per target expected")
        for target, ticket in zip(targets, tickets, strict=True):
            state = "queued"
            for _ in range(_POLL_LIMIT):
                state = await service.poll(f, ticket)
                if state in ("ready", "failed", "gone"):
                    break
            if state != "ready":
                problems.append(f"ticket never became ready (last state {state})")
                continue
            before = [u.model_dump_json() for u in await source.list_units(f, d.series)]
            later = [await service.poll(f, ticket) for _ in range(2)]
            after = [u.model_dump_json() for u in await source.list_units(f, d.series)]
            if later != ["ready", "ready"]:
                problems.append(f"poll must not change a ready ticket (then saw {later})")
            if before != after:
                problems.append("poll must not change what the service lists")
            payload = await service.fetch(f, ticket)
            if isinstance(target, RangeRef) != isinstance(payload, RangedPayload):
                problems.append(
                    "a range target must yield a RangedPayload and a unit a UnitPayload"
                )
            if not _roundtrips(payload):
                problems.append("fetched payload does not round-trip")
        await service.cancel(f, tickets[0])
        report.add("server_queue", not problems, "; ".join(problems))

    async def health() -> None:
        result = await cast(Any, source).semantic_health(f)
        report.add("health_probe", _roundtrips(result), "health does not round-trip")

    if not units:
        report.add("units_present", False, "the synthetic fixtures must yield units")
        return
    if Capability.RANGED_CONTENT in caps:
        await guarded("ranged_content", ranged)
    else:
        await guarded("fetch_unit", single)
    if queue:
        await guarded("server_queue", queued)
    if isinstance(connected, ConnectedCapability) and ConnectedCapability.HEALTH_PROBE in connected:
        await guarded("health_probe", health)


async def _check_reinstall(
    source: Source,
    f: Fetcher,
    first: _Discovery,
    reinstall: Callable[[], Source] | None,
    series_ref: SeriesRef | None,
    report: ConformanceReport,
    guarded: Callable[[str, Callable[[], Awaitable[None]]], Awaitable[None]],
) -> None:
    connected = isinstance(getattr(source, "connected", None), ConnectedCapability)
    if reinstall is None:
        if connected:  # a plain source has no tool to reinstall; a connected service does
            report.add(
                "simulated_reinstall",
                False,
                "a connected service needs a reinstall factory (EF-SRC-036)",
            )
        return

    async def replay() -> None:
        fresh = reinstall()
        if fresh is source:
            report.add("simulated_reinstall", False, "the factory must return a new instance")
            return
        again = await _discover(fresh, f, series_ref)
        problems = []
        if [(r.source_key, r.remote_id) for r in first.refs] != [
            (r.source_key, r.remote_id) for r in again.refs
        ]:
            problems.append("series identities (source_key, remote_id) changed")
        if [u.remote_unit_id for u in first.units] != [u.remote_unit_id for u in again.units]:
            problems.append("remote_unit_id changed")
        report.add(
            "simulated_reinstall",
            not problems,
            "; ".join(problems) + ": new Variants would appear" if problems else "",
        )

    await guarded("simulated_reinstall", replay)


def assert_conforms(
    source: Source,
    fetcher: Fetcher,
    *,
    reinstall: Callable[[], Source] | None = None,
    series_ref: SeriesRef | None = None,
) -> ConformanceReport:
    """Synchronous entry point for a plugin's own tests: raises AssertionError on any failure."""
    report = asyncio.run(
        run_source_conformance(source, fetcher, reinstall=reinstall, series_ref=series_ref)
    )
    report.assert_ok()
    return report


__all__ = [
    "CheckResult",
    "ConformanceReport",
    "ConnectedService",
    "assert_conforms",
    "run_source_conformance",
]
