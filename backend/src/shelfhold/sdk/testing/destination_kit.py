# SPDX-License-Identifier: MIT
"""The destination chapter of the conformance kit (ENF-QUAL-003, EF-EXP-058, EF-EXP-065).

Same kit, same `ConformanceReport` as `shelfhold.sdk.testing.kit`: this module holds what only a
destination needs. It replays a destination on synthetic fixtures, offline, and touches neither the
disk nor the network: a destination describes (`FsOpPlan`, `ApiCall`), the host executes.

What it plays:

* the declarations: `transport`, `egress`, `access_partitioning`, `readback`, `can_purge`;
* the plan and the calls the destination returned for a synthetic item, passed through the same
  functions as the host (JSON round trip, then the check that runs just before each operation);
* the events the destination names, which must stay in `plugin.<key>.*`;
* that nothing is written outside the binding root, under `.shelfhold/**`, or as a sidecar the
  destination says harms it.

It imports nothing of the host: a third-party plugin runs it from `shelfhold-sdk` alone.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import ValidationError

from shelfhold.sdk.api_call import ApiCall, ApiCallError, check_api_call, parse_api_call
from shelfhold.sdk.events import PluginEventError, check_plugin_event, plugin_event_type
from shelfhold.sdk.export_target import TargetCapabilities, sidecar_forbidden
from shelfhold.sdk.fsops import (
    FsOp,
    FsOpPlan,
    FsOpPlanError,
    PlannedContent,
    check_destination_operation,
    check_destination_plan_before_run,
    parse_destination_plan,
)
from shelfhold.sdk.testing.kit import (
    ConformanceReport,
    run_destination_declaration_check,
    run_indexer_plan_check,
)

SYNTHETIC_ITEM: Final = "synthetic-item"
"""The `export_item` the kit plans for the replay: a destination may cite this one."""
SYNTHETIC_WATERMARKED: Final = "synthetic-watermarked"
"""A planned but watermarked `export_item`: it never leaves for an `externe` destination."""
SYNTHETIC_LIBRARY: Final = "synthetic-library"
SYNTHETIC_PATH: Final = "Synthetic Series"

TRANSPORTS: Final = ("tree", "dropbox", "push", "pull")
EGRESSES: Final = ("foyer", "externe")
PARTITIONINGS: Final = ("none", "library", "user")
READBACKS: Final = ("none", "verify", "fetch")


@dataclass(frozen=True)
class DestinationScenario:
    """What the destination returned for the synthetic item, which the kit replays.

    `plan` is an `FsOpPlan`, or the JSON (text or dict) a plugin handed over: a plan that cannot
    even be built is then reported by name instead of raising. `calls` are extra `ApiCall`s; the
    kit also replays `scan_calls`, `inventory_calls` and `update_metadata` presence itself.
    `events` are the event types the destination emits, `key` the plugin key of its space.
    """

    key: str
    plan: object | None = None
    calls: tuple[object, ...] = ()
    events: tuple[str, ...] = ()


def _why(error: Exception) -> str:
    """A reason without the offending value (a call can carry a secret)."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}"
            for e in error.errors(include_input=False, include_url=False, include_context=False)
        )
    return str(error) or type(error).__name__


def _declared(
    report: ConformanceReport, caps: Any, name: str, allowed: Iterable[str], check: str
) -> None:
    allowed = tuple(allowed)
    if not hasattr(caps, name):
        report.add(check, False, f"{name} is not declared ({', '.join(allowed)})")
        return
    value = getattr(caps, name)
    report.add(check, value in allowed, f"{name} {value!r} is not one of {', '.join(allowed)}")


def _check_declarations(report: ConformanceReport, caps: Any) -> None:
    _declared(report, caps, "transport", TRANSPORTS, "transport_declared")
    _declared(report, caps, "egress", EGRESSES, "egress_declared")
    _declared(report, caps, "access_partitioning", PARTITIONINGS, "access_partitioning_declared")
    _declared(report, caps, "readback", READBACKS, "readback_declared")
    purge = getattr(caps, "can_purge", None)
    report.add(
        "can_purge_declared",
        isinstance(purge, bool),
        "can_purge is not declared (a boolean: permanent deletion, apart from the bin)"
        if purge is None
        else f"can_purge {purge!r} is not a boolean",
    )
    if isinstance(caps, TargetCapabilities):
        try:
            same = TargetCapabilities.model_validate_json(caps.model_dump_json()) == caps
        except ValueError as error:
            same, detail = False, _why(error)
        else:
            detail = "the capabilities do not survive a JSON round trip"
        report.add("capabilities_roundtrip_json", same, detail)
    declared = getattr(caps, "sidecars", None) or set()
    forbidden = getattr(caps, "forbidden_sidecars", None) or ()
    clash = sorted(s for s in declared if sidecar_forbidden(forbidden, s))
    report.add(
        "no_forbidden_sidecar",
        not clash,
        f"it declares sidecar(s) it also forbids: {', '.join(clash)}",
    )


def _plan_ops(
    report: ConformanceReport, raw: object
) -> tuple[FsOpPlan | None, tuple[FsOp, ...] | None]:
    """The plan as the host would read it, and its operations when they could be read at all."""
    if isinstance(raw, FsOpPlan):
        data: Any = json.loads(raw.model_dump_json())
    elif isinstance(raw, str | bytes | bytearray):
        try:
            data = json.loads(raw)
        except ValueError:
            report.add("plan_roundtrips_json", False, "the plan cannot be read as JSON")
            return None, None
    else:
        data = raw
    try:
        ops = tuple(FsOp.model_validate(op) for op in data["ops"])
    except (ValidationError, KeyError, TypeError) as error:
        report.add("plan_roundtrips_json", False, f"the plan cannot be read: {_why(error)}")
        return None, None
    problems = []
    for index, op in enumerate(ops):
        try:
            check_destination_operation(op)
        except FsOpPlanError as error:
            problems.append(f"operation #{index} ({op.op}): {error}")
    report.add("plan_inside_root_outside_host_area", not problems, "; ".join(problems))
    if problems:
        return None, ops
    try:
        plan = parse_destination_plan(raw)
        again = parse_destination_plan(plan.model_dump_json())
    except (ValidationError, FsOpPlanError, ValueError) as error:
        report.add("plan_roundtrips_json", False, f"the plan cannot be read: {_why(error)}")
        return None, ops
    report.add("plan_roundtrips_json", again == plan, "the plan changes through JSON")
    return plan, ops


def _written_names(ops: tuple[FsOp, ...]) -> list[tuple[int, str]]:
    names = []
    for index, op in enumerate(ops):
        target = op.dst_rel_path if op.op == "rename" else op.rel_path
        if op.op in ("write", "write_inline", "rename") and target:
            names.append((index, target.rsplit("/", 1)[-1]))
    return names


def _check_plan(report: ConformanceReport, caps: Any, raw: object, planned: PlannedContent) -> None:
    plan, ops = _plan_ops(report, raw)
    if ops is not None:
        forbidden = getattr(caps, "forbidden_sidecars", None) or ()
        hits = [
            f"operation #{i} writes forbidden sidecar {name!r}"
            for i, name in _written_names(ops)
            if sidecar_forbidden(forbidden, name)
        ]
        report.add("no_forbidden_sidecar", not hits, "; ".join(hits))
    if plan is None:
        return
    try:
        check_destination_plan_before_run(plan, planned=planned)
        problem = ""
    except FsOpPlanError as error:
        problem = str(error)
    report.add("plan_checked_before_run", not problem, problem)
    if getattr(caps, "indexes_tree", False) is True:
        report.checks.extend(run_indexer_plan_check(caps, plan).checks)


def _replayed_calls(destination: Any, extra: tuple[object, ...]) -> list[tuple[str, object]]:
    labelled: list[tuple[str, object]] = [("scenario", call) for call in extra]
    replays = (("scan_calls", [SYNTHETIC_PATH]), ("inventory_calls", SYNTHETIC_LIBRARY))
    for name, argument in replays:
        method = getattr(destination, name, None)
        if callable(method):
            labelled += [(name, call) for call in method(argument)]
    return labelled


def _check_calls(
    report: ConformanceReport,
    destination: Any,
    caps: Any,
    scenario: DestinationScenario,
    planned: PlannedContent,
) -> None:
    unreadable: list[str] = []
    refused: list[str] = []
    try:
        calls = _replayed_calls(destination, scenario.calls)
    except Exception as error:
        calls = []
        unreadable.append(f"a call method raised {type(error).__name__}")
    purge = getattr(caps, "can_purge", False) is True
    for origin, raw in calls:
        try:
            call = parse_api_call(raw)
            same = parse_api_call(call.model_dump_json()) == call
        except (ValidationError, ApiCallError, ValueError, TypeError) as error:
            unreadable.append(f"{origin}: cannot be read as an ApiCall ({_why(error)})")
            continue
        if not same:
            unreadable.append(f"{origin}: a call changes through JSON")
        try:
            check_api_call(call, planned, can_purge=purge)
        except ApiCallError as error:
            refused.append(f"{origin} {call.method} {call.path.split('?')[0]}: {error}")
    if calls or unreadable:
        report.add("calls_roundtrip_json", not unreadable, "; ".join(unreadable))
        report.add("calls_checked_before_run", not refused, "; ".join(refused))


def _check_readback(report: ConformanceReport, destination: Any, caps: Any) -> None:
    if getattr(caps, "readback", "none") not in ("verify", "fetch"):
        return
    method = getattr(destination, "inventory_calls", None)
    problem = ""
    if not callable(method):
        problem = f"readback {caps.readback!r} needs inventory_calls to read a copy back"
    elif not list(method(SYNTHETIC_LIBRARY)):
        problem = f"readback {caps.readback!r} but inventory_calls describes no call"
    report.add("readback_has_a_way_to_read", not problem, problem)


def _check_events(report: ConformanceReport, scenario: DestinationScenario) -> None:
    outside = []
    for event in scenario.events:
        try:
            check_plugin_event(scenario.key, event)
        except PluginEventError:
            outside.append(event)
    detail = ""
    if outside:
        try:
            space = plugin_event_type(scenario.key, "*")
        except PluginEventError:
            space = "plugin.<key>.*"
        detail = f"event(s) outside {space}: {', '.join(outside)}"
    report.add("events_in_plugin_namespace", not outside, detail)


def _planned_for(caps: Any) -> PlannedContent:
    """What the kit plans: the egress is the one the destination declares, as the instance would
    attest it, and one planned file is watermarked."""
    egress: Literal["foyer", "externe"] = (
        "externe" if getattr(caps, "egress", None) == "externe" else "foyer"
    )
    return PlannedContent(
        planned_refs=frozenset({SYNTHETIC_ITEM, SYNTHETIC_WATERMARKED}),
        egress=egress,
        watermarked_refs=frozenset({SYNTHETIC_WATERMARKED}),
    )


def run_destination_conformance(
    destination: Any, scenario: DestinationScenario
) -> ConformanceReport:
    """Replay `destination` on `scenario` and report every check, passed or not."""
    report = ConformanceReport()
    caps = getattr(destination, "capabilities", None)
    if caps is None:
        report.add("capabilities_declared", False, "the destination has no `capabilities`")
        return report
    report.checks.extend(run_destination_declaration_check(caps).checks)
    _check_declarations(report, caps)
    _check_readback(report, destination, caps)
    update = getattr(caps, "update_metadata", None)
    if update is not None:
        report.add(
            "update_metadata_is_implemented",
            callable(getattr(destination, "update_metadata", None)),
            "update_metadata is declared but the destination has no update_metadata method",
        )
    planned = _planned_for(caps)
    report.add(
        "scenario_replays_something",
        scenario.plan is not None or bool(scenario.calls),
        "the scenario replays nothing: give the plan or the calls returned for the synthetic item",
    )
    if scenario.plan is not None:
        _check_plan(report, caps, scenario.plan, planned)
    _check_calls(report, destination, caps, scenario, planned)
    _check_events(report, scenario)
    return report


def assert_destination_conforms(
    destination: Any, scenario: DestinationScenario
) -> ConformanceReport:
    """`AssertionError` naming each failed check, else the report."""
    report = run_destination_conformance(destination, scenario)
    report.assert_ok()
    return report


__all__ = [
    "SYNTHETIC_ITEM",
    "SYNTHETIC_WATERMARKED",
    "ApiCall",
    "DestinationScenario",
    "assert_destination_conforms",
    "run_destination_conformance",
]
