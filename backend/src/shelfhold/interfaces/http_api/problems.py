"""RFC 9457 problem details: one shape for every error the API returns (EF-INT-001).

The `type` is the stable URI of the catalog code (EF-UX-003), `title` and `action` come from the
French catalog. `detail` is written by the raising code, never taken from an exception message:
nothing internal (path, SQL, trace) can reach a client. The request payload is never echoed.
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

from shelfhold.error_catalog import ErrorCode
from shelfhold.i18n import t
from shelfhold.infrastructure.http_headers import apply_security_headers
from shelfhold.interfaces.http_api.catalog import all_error_codes, error_code

PROBLEM_MEDIA_TYPE = "application/problem+json"
REQUEST_ID_HEADER = "X-Request-ID"  # kept in step with middleware.REQUEST_ID_HEADER


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


_STATUS_TO_CODE = {400: "bad_request", 404: "not_found", 405: "method_not_allowed"}


class Problem(BaseModel):
    """RFC 9457 body, as documented in the OpenAPI schema."""

    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    code: str
    action: str
    detail: str | None = None
    request_id: str | None = None


class ProblemError(Exception):
    """Raise from a route or a dependency to answer with a problem."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        detail: str | None = None,
        extensions: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(code)  # the message is the stable code, never client-visible detail
        self.spec: ErrorCode = error_code(code)
        self.status = status or self.spec.status
        self.detail = detail
        self.extensions = dict(extensions or {})
        self.headers = dict(headers or {})


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def problem_body(
    spec: ErrorCode,
    *,
    status: int | None = None,
    detail: str | None = None,
    request_id: str | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": spec.type_uri,
        "title": t(spec.title_key),
        "status": status or spec.status,
        "code": spec.code,
        "action": t(spec.action_key),
    }
    if detail:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    for name, value in (extensions or {}).items():
        body.setdefault(name, value)  # an extension never overrides a standard member
    return body


def build_problem_response(
    spec: ErrorCode,
    *,
    request_id: str | None,
    status: int | None = None,
    detail: str | None = None,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """A complete problem response, discretion headers included (usable outside the app stack)."""
    body = problem_body(
        spec, status=status, detail=detail, request_id=request_id, extensions=extensions
    )
    response = JSONResponse(
        body, status_code=body["status"], media_type=PROBLEM_MEDIA_TYPE, headers=dict(headers or {})
    )
    apply_security_headers(response.headers)
    if request_id:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


def problem_response(request: Request, error: ProblemError) -> JSONResponse:
    return build_problem_response(
        error.spec,
        request_id=_request_id(request),
        status=error.status,
        detail=error.detail,
        extensions=error.extensions,
        headers=error.headers,
    )


def _validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    # Only where and what kind: pydantic's `input`, `ctx` and `msg` can echo the payload.
    return [{"loc": [str(part) for part in e["loc"]], "type": str(e["type"])} for e in errors]


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    def _problem(request: Request, exc: Exception) -> JSONResponse:
        error = cast(ProblemError, exc)
        return problem_response(request, error)

    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, exc: Exception) -> JSONResponse:
        error = cast(RequestValidationError, exc)
        return problem_response(
            request,
            ProblemError(
                "validation_failed", extensions={"errors": _validation_errors(error.errors())}
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: Exception) -> Response:
        error = cast(StarletteHTTPException, exc)
        if not is_api_path(request.url.path):
            return await http_exception_handler(request, error)  # the SPA side stays plain
        code = _STATUS_TO_CODE.get(
            error.status_code, "bad_request" if error.status_code < 500 else "internal_error"
        )
        return problem_response(
            request, ProblemError(code, status=error.status_code, headers=error.headers)
        )

    @app.exception_handler(Exception)
    def _unhandled(request: Request, _exc: Exception) -> JSONResponse:
        # The trace is logged by RequestContextMiddleware (request_id bound), never sent.
        return problem_response(request, ProblemError("internal_error"))


def default_problem_responses() -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` shared by every operation: the default answer is a problem."""
    return {"default": {"model": Problem, "description": t("api.problem_description")}}


def _problem_ref(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
    }


def install_problem_openapi(app: FastAPI, *, public_paths: frozenset[str] = frozenset()) -> None:
    """Document problems as `application/problem+json` and drop FastAPI's own 422 schema.

    Every catalog code is listed under `x-problem-types` (its `type` URI and status), so that
    clients find `reauth-required` and `credential-not-allowed` in the v1 document (EF-USR-022,
    EF-USR-024). Every `/api/` operation but the public ones documents 401 and 403.
    """
    original = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        for item in schema.get("paths", {}).values():
            for operation in item.values():
                responses = operation.get("responses", {})
                responses.pop("422", None)  # covered by `default` (validation_failed)
                default = responses.get("default")
                if default and "application/json" in default.get("content", {}):
                    default["content"] = {
                        PROBLEM_MEDIA_TYPE: default["content"]["application/json"]
                    }
        for path, item in schema.get("paths", {}).items():
            if not is_api_path(path) or path in public_paths:
                continue
            for operation in item.values():
                responses = operation.setdefault("responses", {})
                responses.setdefault("401", _problem_ref(t("api.problem_401")))
                responses.setdefault("403", _problem_ref(t("api.problem_403")))
        for name in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(name, None)
        schema["x-problem-types"] = {
            spec.type_uri: {"code": spec.code, "status": spec.status}
            for spec in sorted(all_error_codes(), key=lambda s: s.code)
        }
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
