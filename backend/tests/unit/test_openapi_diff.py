"""The OpenAPI comparison rules (tools/openapi_diff.py): what is a breaking change."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.tools_loader import load_tool

od = load_tool("openapi_diff")

BASE: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Shelfhold", "version": "1"},
    "paths": {
        "/a": {
            "get": {
                "parameters": [
                    {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "ok"}, "404": {"description": "none"}},
            },
            "post": {
                "requestBody": {"required": False, "content": {}},
                "responses": {"202": {"description": "accepted"}},
            },
        }
    },
    "components": {
        "schemas": {
            "Out": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer"},
                    "state": {"enum": ["a", "b"], "type": "string"},
                },
            }
        }
    },
}


def _with(edit: Any) -> list[Any]:
    new = copy.deepcopy(BASE)
    edit(new)
    return od.diff(BASE, new)


def test_identical_documents_have_no_change() -> None:
    assert od.diff(BASE, copy.deepcopy(BASE)) == []


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["paths"].pop("/a"),
        lambda d: d["paths"]["/a"].pop("post"),
        lambda d: d["paths"]["/a"]["get"]["responses"].pop("404"),
        lambda d: d["paths"]["/a"]["get"].pop("parameters"),
        lambda d: d["paths"]["/a"]["get"]["parameters"].append(
            {"name": "r", "in": "query", "required": True, "schema": {"type": "string"}}
        ),
        lambda d: d["paths"]["/a"]["post"]["requestBody"].update(required=True),
        lambda d: d["components"]["schemas"]["Out"]["properties"].pop("id"),
        lambda d: d["components"]["schemas"]["Out"]["properties"]["id"].update(type="string"),
        lambda d: d["components"]["schemas"]["Out"]["required"].append("state"),
        lambda d: d["components"]["schemas"]["Out"]["properties"]["state"]["enum"].pop(),
        lambda d: d["components"]["schemas"].pop("Out"),
    ],
    ids=[
        "path removed", "method removed", "status removed", "parameters removed",
        "required parameter added", "body becomes required", "property removed",
        "type changed", "property becomes required", "enum value removed", "schema removed",
    ],
)  # fmt: skip
def test_incompatible_change_is_breaking(edit: Any) -> None:
    changes = _with(edit)
    assert changes
    assert od.breaking(changes), [str(c) for c in changes]


@pytest.mark.parametrize(
    "edit",
    [
        lambda d: d["paths"].update({"/b": {"get": {"responses": {"200": {"description": "x"}}}}}),
        lambda d: d["paths"]["/a"]["get"]["responses"]["200"].update(description="changed"),
        lambda d: d["paths"]["/a"]["get"]["responses"].update({"500": {"description": "boom"}}),
        lambda d: d["components"]["schemas"]["Out"]["properties"].update({"x": {"type": "string"}}),
        lambda d: d["components"]["schemas"]["Out"]["properties"]["state"]["enum"].append("c"),
        lambda d: d["info"].update(version="2"),
    ],
    ids=["path added", "description", "status added", "optional property", "enum value", "version"],
)
def test_compatible_change_is_reported_but_not_breaking(edit: Any) -> None:
    changes = _with(edit)
    assert changes
    assert not od.breaking(changes), [str(c) for c in changes]
