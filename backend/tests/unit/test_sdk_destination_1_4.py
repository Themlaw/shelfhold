"""SDK 1.4, destination contract (M4 L03): ApiCall body, optional update_metadata, readback,
can_purge, and the push transport kept behind the kit. A 1.3 destination still loads unchanged.

Every secret below is a made-up placeholder, never a real credential.
"""

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import shelfhold.sdk as sdk_package
from shelfhold.sdk import export_target
from shelfhold.sdk.api_call import (
    ApiCall,
    ApiCallError,
    ContentBody,
    JsonBody,
    MultipartEncoding,
    RawEncoding,
    check_api_call,
)
from shelfhold.sdk.export_target import (
    ExportTarget,
    MetadataUpdateSupport,
    TargetCapabilities,
    copy_can_serve_bytes,
    copy_can_verify,
    metadata_change_route,
)
from shelfhold.sdk.fsops import (
    ContentRef,
    FsOp,
    FsOpPlan,
    FsOpPlanError,
    PlannedContent,
    check_destination_plan,
)
from shelfhold.sdk.models import SDK_VERSION, sdk_supports
from shelfhold.sdk.testing.kit import run_destination_declaration_check

FAKE_SECRET = "fake-secret-0000-not-a-credential"  # noqa: S105 - a placeholder for the tests


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


def _planned(**over: Any) -> PlannedContent:
    base: dict[str, Any] = {"planned_refs": {"item-1"}, "egress": "foyer"}
    return PlannedContent.model_validate(base | over)


def _content_call(ref: str = "item-1", kind: str = "export_item", **encoding: Any) -> ApiCall:
    return ApiCall(
        method="POST",
        path="/api/upload",
        auth="target_admin",
        body=ContentBody(content_ref=ContentRef(kind=kind, ref=ref), **encoding),  # type: ignore[arg-type]
    )


# --- ApiCall.body: the same rules as FsOp.write ---------------------------------------------


@pytest.mark.req("EF-EXP-001")
def test_apicall_content_body_obeys_fsop_reference_and_egress_rules() -> None:
    ok = _content_call()
    check_api_call(ok, _planned())  # a planned export_item, raw: accepted

    # a reference that is not an export_item planned for this call and this binding
    with pytest.raises(ApiCallError):
        check_api_call(_content_call("item-2"), _planned())
    with pytest.raises(ApiCallError):
        check_api_call(_content_call(kind="sidecar"), _planned())
    # a watermarked file towards an external destination, never towards the home
    watermarked = _planned(watermarked_refs={"item-1"})
    check_api_call(ok, watermarked)
    with pytest.raises(ApiCallError):
        check_api_call(ok, watermarked.model_copy(update={"egress": "externe"}))
    # the provenance mirror is never sent, planned or not
    with pytest.raises(ApiCallError):
        check_api_call(ok, _planned(mirror_refs={"item-1"}))
    # the very same rule governs FsOp.write: one function, two callers
    write = FsOpPlan(ops=(FsOp(op="write", rel_path="a.cbz", content_ref=ok.body.content_ref),))  # type: ignore[union-attr]
    check_destination_plan(write, planned=_planned())
    for refused in (
        _planned(planned_refs=set()),
        watermarked.model_copy(update={"egress": "externe"}),
        _planned(mirror_refs={"item-1"}),
    ):
        with pytest.raises(FsOpPlanError):
            check_destination_plan(write, planned=refused)


@pytest.mark.req("EF-EXP-001")
@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/x",
        "//evil.example/x",
        "http:/x",
        "api/no-leading-slash",
        "/api/../admin",
        "/api/./x",
        "/api/x?apikey=a",
        "/api/x#frag",
        "/api\\x",
        "/api/\x00x",
        "/user@host/x",
        "",
    ],
)
def test_apicall_path_is_relative_only_the_host_is_fixed_by_configuration(path: str) -> None:
    call = ApiCall(method="POST", path=path, auth="target_admin", body=JsonBody(value={"a": 1}))
    with pytest.raises(ApiCallError):
        check_api_call(call, _planned())


@pytest.mark.req("EF-EXP-001")
def test_apicall_body_shapes_roundtrip_and_json_body_stays_accepted() -> None:
    legacy = ApiCall(method="POST", path="/api/x", json_body={"a": 1}, auth="target_admin")
    check_api_call(legacy, _planned())  # 1.3 shape, still fine
    assert legacy.body is None

    multipart = _content_call(
        encoding=MultipartEncoding(file_field="file", fields={"name": "Vol 1"})
    )
    for call in (
        legacy,
        multipart,
        _content_call(encoding=RawEncoding(content_type="application/zip")),
        ApiCall(method="PUT", path="/api/x", auth="none", body=JsonBody(value=[1, 2])),
    ):
        assert ApiCall.model_validate_json(call.model_dump_json()) == call
    check_api_call(multipart, _planned())

    # a JSON body and a content body together, a content body on GET, a headerless auth: refused
    with pytest.raises(ValueError):
        ApiCall(
            method="POST", path="/x", auth="none", json_body={}, body=JsonBody(value={})
        )
    with pytest.raises(ApiCallError):
        check_api_call(
            ApiCall(
                method="GET",
                path="/x",
                auth="none",
                body=ContentBody(content_ref=ContentRef(kind="export_item", ref="item-1")),
            ),
            _planned(),
        )
    with pytest.raises(ValueError):  # the credential is injected by the core: no field for it
        ApiCall.model_validate(
            {"method": "GET", "path": "/x", "auth": "none", "headers": {"Authorization": "x"}}
        )


@pytest.mark.req("EF-EXP-001", "ENF-SEC-003")
def test_apicall_body_never_leaks_a_secret_in_repr_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    call = ApiCall(
        method="POST",
        path="/api/x",
        query={"apikey": FAKE_SECRET},
        auth="target_admin",
        body=ContentBody(
            content_ref=ContentRef(kind="export_item", ref="item-1"),
            encoding=MultipartEncoding(file_field="file", fields={"token": FAKE_SECRET}),
        ),
    )
    json_call = ApiCall(
        method="POST", path="/api/x", auth="none", body=JsonBody(value={"k": FAKE_SECRET})
    )
    for obj in (call, json_call, call.body, json_call.body):
        assert FAKE_SECRET not in repr(obj)
        assert FAKE_SECRET not in str(obj)
    # a validation error carries no input value
    with pytest.raises(ValidationError) as caught:
        ApiCall.model_validate(
            {"method": "POST", "path": "/x", "auth": "bogus", "query": {"k": FAKE_SECRET}}
        )
    assert FAKE_SECRET not in str(caught.value)
    # a refusal of the host names the rule, never the value
    bad = ApiCall(
        method="POST",
        path=f"/api/x?apikey={FAKE_SECRET}",
        auth="target_admin",
        body=JsonBody(value={"k": FAKE_SECRET}),
    )
    with pytest.raises(ApiCallError) as refused:
        check_api_call(bad, _planned())
    assert FAKE_SECRET not in str(refused.value)
    assert FAKE_SECRET not in caplog.text


# --- update_metadata: optional ---------------------------------------------------------------


@pytest.mark.req("EF-EXP-001")
def test_destination_without_update_metadata_falls_back_to_rewrite_proposal() -> None:
    without = _caps()
    assert without.update_metadata is None  # absent means not supported
    route = metadata_change_route(without, {"title"})
    assert route.update == set()
    assert route.rewrite_proposal == {"title"}
    assert route.warn_rewrite  # the warning of EF-EXP-037

    partial = _caps(update_metadata=MetadataUpdateSupport(fields={"title", "summary"}, lock=True))
    route = metadata_change_route(partial, {"title", "cover"})
    assert route.update == {"title"}
    assert route.rewrite_proposal == {"cover"}
    assert route.warn_rewrite
    full = metadata_change_route(partial, {"summary"})
    assert full.update == {"summary"} and not full.rewrite_proposal and not full.warn_rewrite
    assert not metadata_change_route(without, set()).warn_rewrite  # nothing changed, nothing said


@pytest.mark.req("EF-EXP-001")
def test_update_metadata_is_not_part_of_the_required_protocol() -> None:
    assert "update_metadata" not in ExportTarget.__protocol_attrs__  # type: ignore[attr-defined]
    assert hasattr(export_target, "MetadataUpdater")  # the optional capability, apart


# --- readback and can_purge ------------------------------------------------------------------


@pytest.mark.req("EF-EXP-065")
def test_destination_readback_default_none() -> None:
    caps = _caps()
    assert caps.readback == "none"
    assert not copy_can_verify(caps) and not copy_can_serve_bytes(caps)  # never a byte source
    verify = _caps(readback="verify")
    assert copy_can_verify(verify) and not copy_can_serve_bytes(verify)
    fetch = _caps(readback="fetch")
    assert copy_can_verify(fetch) and copy_can_serve_bytes(fetch)
    with pytest.raises(ValidationError):
        _caps(readback="whatever")


@pytest.mark.req("EF-EXP-065")
def test_can_purge_is_distinct_from_can_delete_and_false_by_default() -> None:
    caps = _caps(can_delete=True)
    assert caps.can_delete and caps.can_purge is False
    assert _caps(can_delete=False, can_purge=True).can_purge


# --- the push transport waits behind the kit -------------------------------------------------


@pytest.mark.req("EF-EXP-001", "EF-EXP-065")
def test_kit_refuses_push_transport_before_m4() -> None:
    # guarded: the refusal holds while no destination declares a content body
    refused = run_destination_declaration_check(_caps(transport="push"))
    assert not refused.ok
    assert "transport_push_requires_content_body" in {c.name for c in refused.failures}

    declared = run_destination_declaration_check(_caps(transport="push", content_body=True))
    assert declared.ok, declared.failures
    for transport in ("tree", "dropbox", "pull"):
        assert run_destination_declaration_check(_caps(transport=transport)).ok


@pytest.mark.req("EF-EXP-001")
def test_push_goes_through_the_kit_never_through_a_network_access_of_the_adapter() -> None:
    # the contract modules open no socket and import no HTTP client: the Fetcher and its SSRF
    # guard stay the one way out, and an ApiCall carries a relative path, never a host
    forbidden = {"socket", "ssl", "http", "urllib", "httpx", "httpx2", "requests", "aiohttp",
                 "smtplib", "ftplib"}  # fmt: skip
    root = Path(sdk_package.__file__).parent
    for name in ("export_target.py", "api_call.py", "fsops.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = {
            part.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for part in (
                [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            )
        }
        assert not imported & forbidden, (name, imported & forbidden)
    absolute = ApiCall(method="POST", path="https://other.example/x", auth="none")
    with pytest.raises(ApiCallError):
        check_api_call(absolute, _planned())


# --- N-1 -------------------------------------------------------------------------------------

LEGACY_1_3_CAPABILITIES = json.dumps(
    {
        "media_kinds": ["manga"],
        "formats": ["cbz"],
        "transport": "tree",
        "egress": "foyer",
        "can_update": True,
        "can_delete": True,
        "confirms_arrival": True,
        "reconcilable": True,
        "accepted_content_types": ["cbz"],
        "sidecars": ["comicinfo"],
        "path_scoped_scan": True,
        "access_partitioning": "library",
        "accepted_image_types": ["jpeg"],
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
        "name_normalizer": "legacy-v1",
    }
)


class Legacy13Destination:
    """A destination written for SDK 1.3, kept as it was: no body, no update_metadata."""

    capabilities = TargetCapabilities.model_validate_json(LEGACY_1_3_CAPABILITIES)

    def config_schema(self) -> dict[str, Any]:
        return {}

    def default_template(self, kind: str) -> str:
        return "{series}/{title}"

    def render_sidecars(self, item: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def inventory_calls(self, remote_library_id: str) -> list[ApiCall]:
        return [ApiCall(method="GET", path="/api/inventory", json_body=None, auth="target_admin")]

    def scan_calls(self, remote_paths: list[str]) -> list[ApiCall]:
        return [
            ApiCall(
                method="POST", path="/api/scan", json_body={"paths": remote_paths}, auth="target_admin"
            )
        ]

    def interpret(self, call: ApiCall, response: dict[str, Any]) -> Any:
        return response

    def supported_versions(self) -> dict[str, Any]:
        return {}


@pytest.mark.req("EF-EXP-001", "EF-EXP-065", "ENF-QUAL-003")
def test_export_target_sdk_1_4_is_n_minus_1_compatible() -> None:
    assert SDK_VERSION == "1.4"
    assert sdk_supports("1.3") and sdk_supports("1.4") and not sdk_supports("1.5")
    assert not sdk_supports("2.0")

    legacy = Legacy13Destination()
    caps = legacy.capabilities  # loaded as it was, from its 1.3 JSON
    # every new member has a safe default
    assert caps.can_purge is False and caps.readback == "none"
    assert caps.update_metadata is None and caps.content_body is False
    assert TargetCapabilities.model_validate_json(caps.model_dump_json()) == caps
    # it passes the declaration check of the kit and its calls still validate
    assert run_destination_declaration_check(caps).ok
    for call in (*legacy.inventory_calls("1"), *legacy.scan_calls(["a"])):
        check_api_call(call, _planned())
        assert call.body is None
    # the route of a metadata change for it is the rewrite proposal
    assert metadata_change_route(caps, {"title"}).rewrite_proposal == {"title"}
    # the Kavita-only members stay in the Protocol (removed at SDK 2.0, not before)
    for name in ("parse_name_call", "ensure_library_call", "resolve_remote_calls",
                 "event_subscription", "progress_calls"):  # fmt: skip
        assert hasattr(ExportTarget, name)
