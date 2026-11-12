"""SDK 1.3 freeze (M3 L34): the plugin boundary. Every refusal has a test that tries to cross it."""

import asyncio
import json
import unicodedata
from typing import Any

import pytest
from pydantic import ValidationError

from shelfhold.sdk.events import PluginEventError, check_plugin_event, plugin_event_type
from shelfhold.sdk.export_target import ExportTarget, TargetCapabilities
from shelfhold.sdk.fsops import (
    FsOp,
    FsOpPlan,
    FsOpPlanError,
    check_destination_plan,
    is_reserved_path,
    parse_destination_plan,
)
from shelfhold.sdk.models import (
    SDK_VERSION,
    WAITING_EXITS,
    PluginKind,
    PluginManifest,
    PluginWaitingState,
    sdk_supports,
)
from shelfhold.sdk.source import Source
from shelfhold.sdk.testing.fakes import (
    FakeRangedConnectedService,
    fake_ranged_fetcher,
)
from shelfhold.sdk.testing.kit import run_source_conformance


def _plan(*ops: dict[str, Any]) -> str:
    return json.dumps({"ops": list(ops)})


def _write(path: str, **more: Any) -> dict[str, Any]:
    return {
        "op": "write",
        "rel_path": path,
        "content_ref": {"kind": "export_item", "ref": "i1"},
    } | more


# --- the plan: serialisable, rooted ---------------------------------------------------------


@pytest.mark.req("EF-EXP-001", "EF-EXP-065")
def test_destination_fsop_plan_json_roundtrip_and_rooted() -> None:
    raw = _plan(
        {"op": "mkdir", "rel_path": "Series"},
        _write("Series/Vol 1.cbz"),
        {"op": "rename", "rel_path": "Series/a.cbz", "dst_rel_path": "Series/b.cbz"},
        {"op": "recycle", "rel_path": "Series/old.cbz"},
        {"op": "touch_mtime", "rel_path": "Series/b.cbz", "mtime": "2026-01-02T03:04:05Z"},
    )
    plan = parse_destination_plan(raw)
    assert len(plan.ops) == 5
    assert FsOpPlan.model_validate_json(plan.model_dump_json()) == plan
    check_destination_plan(plan)


@pytest.mark.req("EF-EXP-001", "EF-EXP-065")
@pytest.mark.parametrize(
    "path",
    ["/abs/file", "/", "..", "../x", "a/../../x", "a/..", "a/../b", "C:\\x", "\\\\srv\\s", "c:/x",
     "a\x00b", "", "a//b", "./a", "a/./b", "a\\b", "~/x", "a/"],
)  # fmt: skip
def test_fsop_plan_refuses_unrooted_paths(path: str) -> None:
    with pytest.raises(ValueError):
        parse_destination_plan(_plan(_write(path)))
    with pytest.raises(ValueError):  # the destination of a rename is rooted too
        parse_destination_plan(_plan({"op": "rename", "rel_path": "a", "dst_rel_path": path}))


@pytest.mark.req("EF-EXP-065")
def test_fsop_plan_refuses_an_unknown_field_or_op() -> None:
    with pytest.raises(ValueError):
        parse_destination_plan(_plan(_write("a.cbz", mode="0777")))
    with pytest.raises(ValueError):
        parse_destination_plan(_plan({"op": "chmod", "rel_path": "a"}))
    with pytest.raises(ValueError):
        parse_destination_plan(json.dumps({"ops": [], "root": "/"}))


@pytest.mark.req("EF-EXP-065")
@pytest.mark.parametrize(
    "op",
    [
        {"op": "rename", "rel_path": "a"},
        {"op": "write", "rel_path": "a"},
        {"op": "write_inline", "rel_path": "a"},
        {"op": "touch_mtime", "rel_path": "a"},
        {"op": "mkdir", "rel_path": "a", "dst_rel_path": "b"},
        {"op": "mkdir", "rel_path": "a", "mtime": "2026-01-02T03:04:05Z"},
        {"op": "mkdir", "rel_path": "a" * 5000},
        {"op": "recycle", "rel_path": "a", "content_ref": {"kind": "sidecar", "ref": "x"}},
    ],
)
def test_fsop_plan_refuses_a_malformed_operation(op: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_destination_plan(_plan(op))


RESERVED = [
    ".shelfhold",
    ".SHELFHOLD",
    ".Shelfhold/recycle/x",
    "./.shelfhold",
    "a/../.shelfhold",
    "a/.shelfhold/x",
    ".shelfhold/",
    ".shelfhold\\x",
    ".shelfhold.",
    ".shelfhold ",
    ".shelfhold-mount-id",
    ".shelfhold-probe-1",
    ".\u017fhelfhold",  # long s: folds to "s"
    "\uff0eshelfhold",  # full-width full stop
    unicodedata.normalize("NFD", ".shelfhold"),
    unicodedata.normalize("NFC", ".shelfhold") + "/x",
    ".shel\u0066hold",  # plain letters spelled by code point
    ".shelfhold\u200b",  # zero-width space
]


@pytest.mark.req("EF-EXP-001", "EF-EXP-065")
@pytest.mark.parametrize("path", RESERVED)
def test_fsop_plan_refuses_reserved_shelfhold_paths(path: str) -> None:
    assert is_reserved_path(path)
    for op in (
        _write(path),
        {"op": "mkdir", "rel_path": path},
        {"op": "recycle", "rel_path": path},
        {"op": "touch_mtime", "rel_path": path, "mtime": "2026-01-02T03:04:05Z"},
        {"op": "rename", "rel_path": "a", "dst_rel_path": path},  # moved onto the host area
        {"op": "rename", "rel_path": path, "dst_rel_path": "a"},  # moved out of it
    ):
        with pytest.raises(ValueError):
            parse_destination_plan(_plan(op))


@pytest.mark.req("EF-EXP-065")
@pytest.mark.parametrize("path", ["Shelf/.shelfhold-ok", "shelfhold/x", "my.shelfhold", "a/b.cbz"])
def test_fsop_plan_does_not_refuse_ordinary_names(path: str) -> None:
    if path == "Shelf/.shelfhold-ok":  # a component that starts with the host prefix is reserved
        assert is_reserved_path(path)
        return
    assert not is_reserved_path(path)
    parse_destination_plan(_plan(_write(path)))


# --- unlink belongs to the host ------------------------------------------------------------


@pytest.mark.req("EF-EXP-001", "EF-EXP-065")
def test_fsop_unlink_host_only_never_in_destination_plan() -> None:
    hidden = _plan(_write("a.cbz"), {"op": "unlink", "rel_path": "b.cbz"})  # written by hand
    with pytest.raises(ValueError, match="unlink"):
        parse_destination_plan(hidden)
    # the host keeps the primitive: the operation exists and validates alone
    assert FsOp(op="unlink", rel_path="b.cbz").op == "unlink"
    # a plan built without validation (model_construct) is caught again before execution
    sneaky = FsOpPlan.model_construct(ops=[FsOp(op="unlink", rel_path="b.cbz")])
    with pytest.raises(FsOpPlanError, match="unlink"):
        check_destination_plan(sneaky)
    # and so is one whose operation was swapped after validation
    plan = parse_destination_plan(_plan(_write("a.cbz")))
    object.__setattr__(plan.ops[0], "op", "unlink")
    with pytest.raises(FsOpPlanError):
        check_destination_plan(plan)
    # a reserved path smuggled the same way
    plan2 = parse_destination_plan(_plan(_write("a.cbz")))
    object.__setattr__(plan2.ops[0], "rel_path", ".shelfhold/x")
    with pytest.raises(FsOpPlanError):
        check_destination_plan(plan2)


@pytest.mark.req("EF-EXP-065")
def test_fsop_plan_object_form_and_bound_are_checked_again() -> None:
    assert len(parse_destination_plan({"ops": [{"op": "mkdir", "rel_path": "a"}]}).ops) == 1
    huge = FsOpPlan.model_construct(ops=[FsOp(op="mkdir", rel_path="a")] * 10_001)
    with pytest.raises(FsOpPlanError):
        check_destination_plan(huge)


@pytest.mark.req("EF-EXP-065")
def test_fsop_plan_is_bounded() -> None:
    with pytest.raises(ValueError):
        parse_destination_plan(
            _plan(*[{"op": "mkdir", "rel_path": f"d{n}"} for n in range(10_001)])
        )


# --- the frozen surface ---------------------------------------------------------------------


@pytest.mark.req("EF-EXP-065")
def test_sdk_1_3_frozen_surface() -> None:
    assert FsOp.model_fields["op"].annotation.__args__ == (  # type: ignore[union-attr]
        "mkdir", "write", "write_inline", "rename", "recycle", "touch_mtime", "unlink",
    )  # fmt: skip
    assert set(FsOp.model_fields) == {"op", "rel_path", "dst_rel_path", "content_ref", "mtime"}
    caps = TargetCapabilities.model_fields
    assert caps["can_purge"].default is False and caps["readback"].default == "none"
    assert "render_sidecars" in dir(ExportTarget)


# --- events and waiting states of a plugin --------------------------------------------------


@pytest.mark.req("ENF-OBS-003", "EF-INT-023")
def test_plugin_events_namespaced() -> None:
    assert plugin_event_type("acme", "sync_done") == "plugin.acme.sync_done"
    check_plugin_event("acme", "plugin.acme.sync_done")
    for outside in (
        "plugin.other.sync_done",  # another plugin's space
        "plugin.acmeevil.x",  # a prefix of the key is not the key
        "plugin.acme",  # the space itself
        "plugin.acme.",
        "plugin.acme.a.b",
        "job.failed",  # a core event
        "ops.anything",
        "target.forbidden",
        "PLUGIN.acme.x",
        "plugin.acme.X",
        "plugin..x",
        " plugin.acme.x",
        "plugin.acme.x\n",
    ):
        with pytest.raises(PluginEventError):
            check_plugin_event("acme", outside)
    for bad_key in ("", "a.b", "Acme", "a b", "a/b"):
        with pytest.raises(PluginEventError):
            plugin_event_type(bad_key, "x")
    with pytest.raises(PluginEventError):
        plugin_event_type("acme", "../x")


@pytest.mark.req("ENF-OBS-003")
def test_host_publishes_a_plugin_event_only_under_its_own_key() -> None:
    from shelfhold.plugin_host.registry import publish_plugin_event

    seen: list[tuple[str, dict[str, object]]] = []

    class Bus:
        def publish(
            self, connection: object, event_type: str, payload: Any = None, **kw: Any
        ) -> str:
            seen.append((event_type, dict(payload or {})))
            return "ok"

    publish_plugin_event(Bus(), object(), "acme", "sync_done", {"count": 3})
    assert seen == [("plugin.acme.sync_done", {"count": 3})]
    for name in ("job.failed", "plugin.other.x", "../x", ""):
        with pytest.raises(PluginEventError):
            publish_plugin_event(Bus(), object(), "acme", name, {})
    assert len(seen) == 1  # nothing else reached the bus


@pytest.mark.req("EF-INT-023", "ENF-QUAL-003")
def test_plugin_waiting_state_declares_exit() -> None:
    assert set(WAITING_EXITS) == {"retry", "recheck", "reconfigure"}
    for exit_ in WAITING_EXITS:
        PluginWaitingState(name="login_wall", exit=exit_)
    for bad in (None, "", "ignore", "RETRY", "retry ", 3):
        with pytest.raises(ValidationError):
            PluginWaitingState.model_validate({"name": "login_wall", "exit": bad})
    with pytest.raises(ValidationError):  # no exit at all
        PluginWaitingState.model_validate({"name": "login_wall"})
    for bad_name in ("", "Login", "login wall", "../x", "plugin.x.y", "1a"):
        with pytest.raises(ValidationError):
            PluginWaitingState.model_validate({"name": bad_name, "exit": "retry"})
    with pytest.raises(ValidationError):  # two states with one name
        PluginManifest.model_validate(
            _manifest(waiting_states=[{"name": "w", "exit": "retry"}] * 2)
        )


def _manifest(**more: Any) -> dict[str, Any]:
    return {
        "key": "acme", "kind": PluginKind.SOURCE, "version": "1.0.0", "api": 1, "name": "Acme",
    } | more  # fmt: skip


def _run(service: Any) -> Any:
    return asyncio.run(
        run_source_conformance(
            service, fake_ranged_fetcher(), reinstall=lambda: service.reinstall()
        )
    )


@pytest.mark.req("EF-INT-023", "ENF-QUAL-003")
def test_kit_checks_every_plugin_waiting_state_has_an_exit() -> None:
    good = FakeRangedConnectedService()
    good.manifest = good.manifest.model_copy(
        update={"waiting_states": [PluginWaitingState(name="login_wall", exit="reconfigure")]}
    )
    report = _run(good)
    assert report.ok, report.failures
    assert "waiting_states_declare_exit" in {c.name for c in report.checks}

    # a manifest assembled around the validation: the exit is missing, then unknown
    for broken in (
        PluginWaitingState.model_construct(name="login_wall", exit=None),
        PluginWaitingState.model_construct(name="login_wall", exit="ignore"),
        PluginWaitingState.model_construct(name="Bad Name", exit="retry"),
    ):
        service = FakeRangedConnectedService()
        service.manifest = service.manifest.model_copy(update={"waiting_states": [broken]})
        failed = {c.name for c in _run(service).failures}
        assert "waiting_states_declare_exit" in failed, broken


# --- N-1 and D10 ----------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-036", "ENF-QUAL-003")
def test_sdk_is_1_3_and_a_1_2_plugin_still_loads() -> None:
    assert SDK_VERSION == "1.3"
    assert sdk_supports("1.2") and sdk_supports("1.3") and not sdk_supports("1.4")
    assert not sdk_supports("2.0")


@pytest.mark.req("EF-SRC-036", "ENF-QUAL-003")
def test_a_sdk_1_2_plugin_passes_the_1_3_kit() -> None:
    legacy = FakeRangedConnectedService()  # written for 1.2: implements fetch_range, no exits
    assert callable(legacy.fetch_range) and not legacy.manifest.waiting_states
    report = _run(legacy)
    assert report.ok, report.failures


@pytest.mark.req("EF-SRC-034", "ENF-QUAL-003")
def test_fetch_range_is_optional_and_the_kit_never_calls_it() -> None:
    class NoRange(FakeRangedConnectedService):
        fetch_range = None  # type: ignore[assignment]

    report = _run(NoRange())
    assert report.ok, report.failures
    assert "ranged_content" not in {c.name for c in report.checks}

    calls: list[str] = []

    class Spy(FakeRangedConnectedService):
        async def fetch_range(self, f: Any, target: Any) -> Any:
            calls.append("fetch_range")
            return await super().fetch_range(f, target)

    assert _run(Spy()).ok
    assert calls == []
    assert Source.fetch_range  # the contract keeps the name, with a default
    with pytest.raises(NotImplementedError):
        asyncio.run(Source.fetch_range(object(), None, None))  # type: ignore[arg-type, misc]


@pytest.mark.req("EF-SRC-036", "ENF-QUAL-003")
def test_remote_ids_stable_across_connected_tool_reinstall() -> None:
    from shelfhold.sdk.refs import SeriesRef

    stable = FakeRangedConnectedService()
    assert _run(stable).ok

    class MovesUnits(FakeRangedConnectedService):
        def reinstall(self) -> "MovesUnits":
            return MovesUnits(handle_offset=self.handle_offset + 1000)

        async def list_units(self, f: Any, ref: Any) -> Any:
            units = await super().list_units(f, ref)
            if self.handle_offset:
                return [u.model_copy(update={"remote_unit_id": f"x{self.handle_offset}-{u.number}"})
                        for u in units]  # fmt: skip
            return units

    class MovesSeries(FakeRangedConnectedService):
        def reinstall(self) -> "MovesSeries":
            return MovesSeries(handle_offset=self.handle_offset + 1000)

        def _series(self) -> SeriesRef:
            base = super()._series()
            return base.model_copy(update={"remote_id": f"{base.remote_id}-{self.handle_offset}"})

    for moved in (MovesUnits(), MovesSeries()):
        failed = {c.name for c in _run(moved).failures}
        assert failed == {"simulated_reinstall"}, (type(moved).__name__, failed)
