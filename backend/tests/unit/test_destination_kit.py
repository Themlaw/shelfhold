"""The destination conformity kit (M4 L05, ENF-QUAL-003, EF-EXP-058, EF-EXP-065).

The kit replays a destination on synthetic fixtures. Each rule has a fake destination that must
fail it with a message naming what is wrong. No secret, no network, no real site.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.sdk.api_call import ApiCall, ContentBody
from shelfhold.sdk.export_target import MetadataUpdateSupport, TargetCapabilities
from shelfhold.sdk.fsops import ContentRef, FsOp, FsOpPlan
from shelfhold.sdk.testing.destination_kit import (
    SYNTHETIC_ITEM,
    SYNTHETIC_WATERMARKED,
    DestinationScenario,
    assert_destination_conforms,
    run_destination_conformance,
)
from shelfhold.sdk.testing.fixtures.reserved_targets import (
    AudiobookshelfReservedTarget,
    CalibreReservedTarget,
)

_ITEM = ContentRef(kind="export_item", ref=SYNTHETIC_ITEM)
_PLAN = FsOpPlan(
    ops=(
        FsOp(op="mkdir", rel_path="Serie A"),
        FsOp(op="write", rel_path="Serie A/Volume 1.cbz", content_ref=_ITEM),
    )
)


def _caps(**over: Any) -> TargetCapabilities:
    base: dict[str, Any] = {
        "media_kinds": {"manga"},
        "formats": {"cbz"},
        "transport": "tree",
        "egress": "foyer",
        "can_update": True,
        "can_delete": True,
        "confirms_arrival": True,
        "reconcilable": True,
        "accepted_content_types": ["cbz"],
        "sidecars": {"comicinfo"},
        "path_scoped_scan": True,
        "access_partitioning": "library",
        "accepted_image_types": {"jpeg"},
        "events": "none",
        "collections": "none",
        "progress": "none",
        "manage_libraries": False,
        "naming": {
            "max_segment_bytes": 255,
            "max_path_bytes": 4096,
            "forbidden": "/",
            "case_insensitive": False,
        },
        "name_normalizer": "test-v1",
    }
    return TargetCapabilities.model_validate(base | over)


class _Dest:
    """A fake destination: it describes calls, it executes nothing."""

    def __init__(self, caps: Any) -> None:
        self.capabilities = caps

    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]:
        return [
            ApiCall(method="POST", path="/api/scan", json_body={"path": p}, auth="target_admin")
            for p in remote_paths
        ]


def _without(caps: TargetCapabilities, *names: str) -> SimpleNamespace:
    """The capabilities of a destination that never declared `names` (an older or sloppy plugin)."""
    return SimpleNamespace(**{k: v for k, v in caps.model_dump().items() if k not in names})


def _scenario(**over: Any) -> DestinationScenario:
    base: dict[str, Any] = {"key": "fake-dest", "plan": _PLAN}
    return DestinationScenario(**(base | over))


def _fails(destination: Any, scenario: DestinationScenario, check: str, text: str) -> None:
    report = run_destination_conformance(destination, scenario)
    assert not report.ok
    failed = {c.name: c.detail for c in report.failures}
    assert check in failed, failed
    assert text in failed[check], failed[check]
    with pytest.raises(AssertionError, match=check):
        assert_destination_conforms(destination, scenario)


@pytest.mark.req("ENF-QUAL-003")
def test_a_well_formed_destination_passes_the_kit() -> None:
    report = assert_destination_conforms(_Dest(_caps()), _scenario())
    assert report.ok
    assert {"transport_declared", "plan_roundtrips_json", "readback_declared"} <= {
        c.name for c in report.checks
    }


@pytest.mark.req("ENF-QUAL-003")
def test_kit_replays_nothing_is_a_failure() -> None:
    _fails(_Dest(_caps()), _scenario(plan=None), "scenario_replays_something", "replays nothing")


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-058")
def test_conformity_kit_transport_and_egress() -> None:
    caps = _caps()
    _fails(_Dest(_without(caps, "transport")), _scenario(), "transport_declared", "not declared")
    odd = SimpleNamespace(**(caps.model_dump() | {"transport": "carrier"}))
    _fails(_Dest(odd), _scenario(), "transport_declared", "'carrier'")
    _fails(_Dest(_without(caps, "egress")), _scenario(), "egress_declared", "not declared")
    _fails(
        _Dest(SimpleNamespace(**(caps.model_dump() | {"egress": "maison"}))),
        _scenario(),
        "egress_declared",
        "'maison'",
    )
    # push sends bytes through a call: it needs the declared body, and the kit says so by name
    push = _caps(transport="push", content_body=False)
    _fails(
        _Dest(push), _scenario(), "transport_push_requires_content_body", "needs a declared"
    )
    pushing = _caps(transport="push", content_body=True)
    assert run_destination_conformance(_Dest(pushing), _scenario()).ok


@pytest.mark.req("EF-EXP-058")
def test_kit_never_lets_a_watermarked_file_reach_an_external_egress() -> None:
    mark = ContentRef(kind="export_item", ref=SYNTHETIC_WATERMARKED)
    leaking = FsOpPlan(ops=(FsOp(op="write", rel_path="S/v.cbz", content_ref=mark),))
    _fails(
        _Dest(_caps(egress="externe")),
        _scenario(plan=leaking),
        "plan_checked_before_run",
        "watermarked",
    )
    body = ContentBody(content_ref=mark)
    call = ApiCall(method="POST", path="/api/up", body=body, auth="target_admin")
    pushing = _caps(transport="push", content_body=True, egress="externe")
    _fails(_Dest(pushing), _scenario(plan=None, calls=(call,)), "calls_checked_before_run", "water")
    # the same file towards a destination of the home is the host's choice, not a flaw
    assert run_destination_conformance(_Dest(_caps()), _scenario(plan=leaking)).ok


@pytest.mark.req("EF-EXP-058")
def test_kit_refuses_content_that_was_not_planned() -> None:
    other = ContentRef(kind="export_item", ref="not-planned")
    plan = FsOpPlan(ops=(FsOp(op="write", rel_path="S/v.cbz", content_ref=other),))
    _fails(_Dest(_caps()), _scenario(plan=plan), "plan_checked_before_run", "planned")


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-047")
def test_kit_checks_access_partitioning_declared() -> None:
    caps = _caps()
    _fails(
        _Dest(_without(caps, "access_partitioning")),
        _scenario(),
        "access_partitioning_declared",
        "not declared",
    )
    bad = SimpleNamespace(**(caps.model_dump() | {"access_partitioning": "tenant"}))
    _fails(_Dest(bad), _scenario(), "access_partitioning_declared", "'tenant'")
    for value in ("none", "library", "user"):
        assert run_destination_conformance(
            _Dest(_caps(access_partitioning=value)), _scenario()
        ).ok


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-065")
def test_kit_checks_readback_and_can_purge() -> None:
    caps = _caps()
    _fails(_Dest(_without(caps, "readback")), _scenario(), "readback_declared", "not declared")
    _fails(_Dest(_without(caps, "can_purge")), _scenario(), "can_purge_declared", "not declared")
    wrong = SimpleNamespace(**(caps.model_dump() | {"readback": "peek"}))
    _fails(_Dest(wrong), _scenario(), "readback_declared", "'peek'")
    # a readback that nothing can perform is a promise the host would believe
    _fails(
        _Dest(_caps(readback="verify")), _scenario(), "readback_has_a_way_to_read", "inventory_calls"
    )

    class Reading(_Dest):
        def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
            return [ApiCall(method="GET", path="/api/inventory", auth="target_admin")]

    assert run_destination_conformance(Reading(_caps(readback="fetch")), _scenario()).ok

    # a DELETE without `can_purge` fails, and the message says which call
    delete = ApiCall(method="DELETE", path="/api/series/1", auth="target_admin")
    _fails(
        _Dest(_caps(can_purge=False)),
        _scenario(calls=(delete,)),
        "calls_checked_before_run",
        "DELETE needs a destination that declares can_purge",
    )
    assert run_destination_conformance(
        _Dest(_caps(can_purge=True)), _scenario(calls=(delete,))
    ).ok


@pytest.mark.req("EF-EXP-065")
def test_kit_refuses_plan_outside_namespace() -> None:
    dest = _Dest(_caps())
    _fails(dest, _scenario(events=("job.failed",)), "events_in_plugin_namespace", "job.failed")
    _fails(
        dest,
        _scenario(events=("plugin.other-dest.arrived",)),
        "events_in_plugin_namespace",
        "plugin.other-dest.arrived",
    )
    ok = _scenario(events=("plugin.fake-dest.arrived",))
    assert run_destination_conformance(dest, ok).ok
    # `Fake_Dest` and `fake-dest` are one plugin, one space
    assert run_destination_conformance(dest, _scenario(key="Fake_Dest", events=ok.events)).ok


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-065")
def test_kit_refuses_writes_outside_the_root_or_under_the_host_area() -> None:
    dest = _Dest(_caps())
    host = '{"ops": [{"op": "mkdir", "rel_path": ".shelfhold/x"}]}'
    escape = '{"ops": [{"op": "mkdir", "rel_path": "../outside"}]}'
    absolute = '{"ops": [{"op": "mkdir", "rel_path": "/etc"}]}'
    for raw, word in ((host, "reserved"), (escape, ".."), (absolute, "relative")):
        _fails(dest, _scenario(plan=raw), "plan_inside_root_outside_host_area", word)
    unlink = '{"ops": [{"op": "unlink", "rel_path": "S/v.cbz"}]}'
    _fails(dest, _scenario(plan=unlink), "plan_inside_root_outside_host_area", "unlink")
    _fails(dest, _scenario(plan="not json"), "plan_roundtrips_json", "cannot be read")


@pytest.mark.req("ENF-QUAL-003")
def test_kit_refuses_a_forbidden_sidecar() -> None:
    caps = _caps(forbidden_sidecars=frozenset({"series.json"}))
    sidecar = ContentRef(kind="sidecar", ref="series-json")
    plan = FsOpPlan(
        ops=(FsOp(op="write_inline", rel_path="Serie A/series.json", content_ref=sidecar),)
    )
    _fails(_Dest(caps), _scenario(plan=plan), "no_forbidden_sidecar", "series.json")
    declares = _caps(sidecars={"series.json"}, forbidden_sidecars=frozenset({"series.json"}))
    _fails(_Dest(declares), _scenario(), "no_forbidden_sidecar", "declares")
    assert run_destination_conformance(_Dest(_caps()), _scenario(plan=plan)).ok


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-001")
def test_kit_checks_update_metadata_and_indexer() -> None:
    update = MetadataUpdateSupport(fields={"title"}, lock=False)
    _fails(
        _Dest(_caps(update_metadata=update)),
        _scenario(),
        "update_metadata_is_implemented",
        "update_metadata",
    )

    class Updating(_Dest):
        def update_metadata(self, ref: Any, fields: Any, lock: bool) -> list[ApiCall]:
            return [ApiCall(method="PUT", path="/api/meta", json_body=fields, auth="target_admin")]

    assert run_destination_conformance(Updating(_caps(update_metadata=update)), _scenario()).ok
    indexer = _caps(indexes_tree=True, series_grouping="folder")
    _fails(_Dest(indexer), _scenario(), "indexer_emits_no_fsop_write", "write")


@pytest.mark.req("ENF-QUAL-003")
def test_a_destination_call_that_cannot_round_trip_fails() -> None:
    class Odd(_Dest):
        def scan_calls(self, remote_paths: list[str]) -> list[Any]:
            return [{"method": "TRACE", "path": "/x", "auth": "none"}]

    _fails(Odd(_caps()), _scenario(), "calls_roundtrip_json", "scan_calls")


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-023", "EF-EXP-035")
def test_reserved_destinations_pass_the_kit() -> None:
    abs_target = AudiobookshelfReservedTarget()
    plan = FsOpPlan(ops=(FsOp(op="mkdir", rel_path="Author/Series/1 - Title"),))
    assert run_destination_conformance(abs_target, _scenario(plan=plan)).ok
    calibre = CalibreReservedTarget()
    call = calibre.add_book_call(_ITEM, filename="Title.epub", library_id="main")
    assert run_destination_conformance(calibre, _scenario(plan=None, calls=(call,))).ok


@pytest.mark.req("ENF-QUAL-003", "EF-EXP-065")
def test_kavita_passes_destination_kit() -> None:
    kavita = KavitaDestination()
    caps = kavita.capabilities
    assert (caps.transport, caps.egress, caps.access_partitioning) == ("tree", "foyer", "library")
    assert caps.can_purge is False  # Kavita never purges: only the host's own path erases
    assert "series.json" in caps.forbidden_sidecars
    assert_destination_conforms(kavita, _scenario(key="kavita"))
