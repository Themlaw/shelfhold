"""The credential of an e-reader: OPDS (HTTP Basic) and kosync (`x-auth-user`, `x-auth-key`)
(M4 L46, ADR-0053 §4, EF-USR-008, EF-LEC-009).

A device token is **not** resolved by the authentication middleware: no ordinary route, no
administration route and no reserved action ever sees one, whatever headers a client sends them
(the middleware reads a session cookie or an `Authorization: Bearer shk_…` and nothing else, so
a token presented there is nobody). A route takes it by naming one of the two dependencies below,
and the routes that do are the closed list `DEVICE_ROUTE_PREFIXES`: the dependency itself
refuses a request outside it, so a route mounted elsewhere by mistake never gets a token.

What the dependency gives back is the **subject** profile of the token, read from the database at
this request (role and ceiling are never kept), with the credential `device_token`. Nothing here
writes a library, a secret or a setting.

TLS is required: over plain HTTP (after `http_trusted_proxies` has said what the proxy saw) the
request is refused with `device_tls_required` before any secret is looked at, unless the admin set
`device_allow_plain_http` (a trusted local network, at their risk).

Failures are one answer, 401, for an unknown identifier, a wrong key, a revoked and an expired
token. A wrong guess is counted per (address, identifier) and per address; over the limit the
answer is 429 with `Retry-After`, even for the right key. The address limit alone slows failures
(past it a wrong guess is a 429) and never refuses a right secret.
Nothing about the credential is logged: the audit and the logs name the token by its public
identifier at most, and never the secret, the md5 or the header.
"""

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from starlette.types import Scope

from shelfhold.identity.public import Principal, ThrottledError
from shelfhold.infrastructure.http_headers import BASIC_CHALLENGE
from shelfhold.interfaces.http_api.auth import gate_of
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import (
    client_ip_of,
    client_scheme_of,
    recognise_device_credential,
)

DEVICE_ROUTE_PREFIXES = ("/opds", "/kosync")  # closed: a device token reaches nothing else
_MAX_HEADER = 512


def _refuse(headers: dict[str, str] | None = None) -> ProblemError:
    return ProblemError("invalid_credentials", headers=headers)


def _basic(request: Request) -> tuple[str, str] | None:
    raw = request.headers.get("authorization", "")
    parts = raw.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    value = parts[1]
    if not value.strip() or len(value) > _MAX_HEADER:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, separator, secret = decoded.partition(":")
    return (user, secret) if separator and user and secret else None


def _kosync(request: Request) -> tuple[str, str] | None:
    user = request.headers.get("x-auth-user", "")
    key = request.headers.get("x-auth-key", "")
    if not user or not key or len(user) > _MAX_HEADER or len(key) > _MAX_HEADER:
        return None
    return user, key


def _in_reach(scope: Scope) -> bool:
    """Whether the path, taken from the mount point, is under `/opds` or `/kosync`. A server may
    keep the `root_path` in `path` or not: both are read the same, and a path that only looks like
    one after a different cut (`/x/opds`) is outside."""
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", "")).rstrip("/")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root) :] or "/"
    return any(path == prefix or path.startswith(prefix + "/") for prefix in DEVICE_ROUTE_PREFIXES)


def _resolve(request: Request, *, basic: bool) -> Principal:
    if not _in_reach(request.scope):
        # the closed list holds where the token is read: a route mounted elsewhere never gets one
        raise ProblemError("credential_not_allowed")
    if client_scheme_of(request.scope) != "https" and not getattr(
        request.app.state, "device_allow_plain_http", False
    ):  # before the secret is read: in clear it would travel, and replay, as it is (ADR-0053 §4)
        raise ProblemError("device_tls_required")
    challenge = {"WWW-Authenticate": BASIC_CHALLENGE} if basic else None
    presented = _basic(request) if basic else _kosync(request)
    if presented is None:  # nothing to guess: not counted against anyone
        raise _refuse(challenge)
    public_id, secret = presented
    try:
        principal = gate_of(request).auth.authenticate_device_token(
            public_id,
            ip=client_ip_of(request.scope),
            secret=secret if basic else None,
            md5_key=None if basic else secret,
        )
    except ThrottledError as throttled:
        wait = str(throttled.retry_after_s)
        raise ProblemError(
            "too_many_attempts",
            extensions={"retry_after_s": throttled.retry_after_s},
            headers={"Retry-After": wait},
        ) from None
    if principal is None:
        raise _refuse(challenge)
    recognise_device_credential(request.scope)  # an accepted credential has a budget of its own
    return principal


def require_device_basic(request: Request) -> Principal:
    """OPDS: HTTP Basic, the public identifier as the user name and the secret as the password."""
    return _resolve(request, basic=True)


def require_device_kosync(request: Request) -> Principal:
    """kosync: `x-auth-user` is the public identifier and `x-auth-key` the md5 of the secret."""
    return _resolve(request, basic=False)


def build_device_router() -> APIRouter:
    router = APIRouter(prefix="/kosync", tags=["kosync"])

    @router.get("/users/auth", operation_id="kosync_authorize")
    def authorize(
        response: Response, _principal: Annotated[Principal, Depends(require_device_kosync)]
    ) -> dict[str, str]:
        """What KOReader asks when the reader taps "Login": 200 if the token is good. The other
        routes of kosync (L47) take the same dependency."""
        response.headers["Cache-Control"] = "no-store"
        return {"authorized": "OK"}

    return router
