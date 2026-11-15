"""Adversarial review of the destination contract 1.4 (PR #275, M4 L03): one red proof per hole.

Each test states the behaviour the contract promises. They were red proofs (`xfail(strict=True)`)
on the head of the PR; every hole is closed and the marks went with the fixes. Every secret below
is a made-up placeholder, never a real credential.
"""

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from shelfhold.export.targets.kavita import ProtocolError
from shelfhold.sdk.api_call import (
    ApiCall,
    ApiCallError,
    ContentBody,
    JsonBody,
    RawEncoding,
    check_api_call,
)
from shelfhold.sdk.fsops import (
    ContentRef,
    FsOp,
    FsOpPlan,
    FsOpPlanError,
    PlannedContent,
    check_destination_plan,
)
from tests.kavita_env import harness, run

FAKE_SECRET = "fake-secret-0000-not-a-credential"  # noqa: S105 - a placeholder for the tests
MIRROR = ".shelfhold/manifest/provenance.jsonl"


def _planned(**over: Any) -> PlannedContent:
    base: dict[str, Any] = {
        "planned_refs": {"item-1"},
        "egress": "externe",
        "watermarked_refs": {"item-wm"},
        "mirror_refs": {"mirror-1"},
    }
    return PlannedContent.model_validate(base | over)


def _content_call(**over: Any) -> ApiCall:
    base: dict[str, Any] = {
        "method": "POST",
        "path": "/api/upload",
        "auth": "target_user",
        "body": ContentBody(content_ref=ContentRef(kind="export_item", ref="item-1")),
    }
    return ApiCall(**(base | over))


# -- FsOp plan: the content rule of 1.4 only looks at `write` ----------------------------------


@pytest.mark.parametrize("ref", ["not-planned", "item-wm", "mirror-1"])
def test_write_inline_citing_an_export_item_obeys_the_content_rule(ref: str) -> None:
    plan = FsOpPlan(
        ops=(
            FsOp(
                op="write_inline",
                rel_path="Series/series.json",
                content_ref=ContentRef(kind="export_item", ref=ref),
            ),
        )
    )
    with pytest.raises(FsOpPlanError):
        check_destination_plan(plan, _planned())


# -- ApiCall: the host check fails open on what it does not recognise ---------------------------


def test_check_api_call_refuses_a_body_that_is_not_an_exact_sdk_body() -> None:
    forged = SimpleNamespace(
        kind="content",
        content_ref=ContentRef(kind="export_item", ref=MIRROR),
        encoding=RawEncoding(),
    )
    call = ApiCall.model_construct(
        method="POST", path="/api/upload", auth="target_user", body=forged, json_body=None
    )
    with pytest.raises(ApiCallError):
        check_api_call(call, _planned())


def test_api_call_cannot_be_altered_after_validation() -> None:
    call = _content_call()
    check_api_call(call, _planned())
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        call.path = "//attacker.invalid/x"


@pytest.mark.parametrize("method", ["get ", "TRACE", "CONNECT", "POST\r\nX-Injected: 1"])
def test_check_api_call_refuses_a_method_outside_the_closed_list(method: str) -> None:
    with pytest.raises((ValidationError, ApiCallError)):  # refused at the model, then at the check
        check_api_call(_content_call(method=method), _planned())


# -- ApiCall path: what the guard lets through ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/%252e%252e/admin",  # decoded twice by a proxy in front of the destination: `..`
        "/api/%252f%252e%252e%252fadmin",
        "/api/..;/admin",  # Tomcat and Spring drop the `;` parameter, then resolve `..`
        "/api/\u2025/admin",  # TWO DOT LEADER, NFKC `..`
        "/api/\uff0e\uff0e/admin",  # FULLWIDTH FULL STOP twice, NFKC `..`
        "/api/\uff0fadmin",  # FULLWIDTH SOLIDUS, NFKC `/`
        "/api/%00/admin",  # an encoded NUL
        "/api/\u0085/admin",  # C1 control (NEL), below the 0x20 test only for C0
    ],
)
def test_api_call_path_refuses_encoded_and_unicode_traversal(path: str) -> None:
    with pytest.raises(ApiCallError):
        check_api_call(_content_call(path=path), _planned())


# -- secrets: a guard, not a hole (green on #275): nested validation keeps the values hidden ----


def test_secret_of_a_nested_api_call_never_appears_in_a_validation_error() -> None:
    class CallBatch(BaseModel):  # what a host or the kit does with the list a destination returns
        calls: list[ApiCall]

    bad = {
        "method": "POST",
        "path": "/api/x",
        "auth": "nobody",  # invalid: forces an error on the call
        "query": {"token": FAKE_SECRET},
        "body": {"kind": "json", "value": {"password": FAKE_SECRET}},
    }
    messages = []
    for validate in (
        lambda: TypeAdapter(list[ApiCall]).validate_python([bad]),
        lambda: CallBatch.model_validate({"calls": [bad]}),
    ):
        with pytest.raises(ValidationError) as caught:
            validate()
        messages.append(str(caught.value))
    assert all(FAKE_SECRET not in message for message in messages)


# -- the one way out of Kavita ignores the 1.4 body --------------------------------------------


def test_kavita_client_does_not_silently_drop_a_1_4_body() -> None:
    h = harness()
    run(h.client.probe())
    before = len(h.fake.requests)
    call = ApiCall(
        method="POST",
        path="/api/Library/scan-folder",
        body=JsonBody(value=["not", "an", "object"]),
        auth="target_admin",
    )
    with pytest.raises(ProtocolError):  # as for the same body in `json_body`
        run(h.client.execute(call))
    assert len(h.fake.requests) == before


# -- minor: a media type the header cannot carry -----------------------------------------------


def test_content_type_is_ascii_only() -> None:
    call = _content_call(
        body=ContentBody(
            content_ref=ContentRef(kind="export_item", ref="item-1"),
            encoding=RawEncoding(content_type="application/x-\u00e9pub"),
        )
    )
    with pytest.raises(ApiCallError):
        check_api_call(call, _planned())
