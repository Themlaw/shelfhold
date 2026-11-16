"""`/api/v1/device-tokens`: pairing an e-reader with a profile (M4 L46b, EF-UX-004, EF-USR-022 (4),
ADR-0053 §4). The routes behind the screen "Connect a device".

Creating a token is the reserved action `device_token.issue`: a fresh session in every profile,
never an API key, never a device token (a token is read by `/opds` and `/kosync` only). Revoking is
`device_token.manage`. A member pairs and revokes their own tokens, an admin pairs for a member or
a child (an admin is never the subject of a token) and revokes any, a child manages none. The
secret is in the answer of the creation, once, with `Cache-Control: no-store`; it is not in a URL,
not in a list, not in the audit, and the reserved guard keeps the answer out of the replay store
of `Idempotency-Key`. A token of another profile answers like one that is not there.

The setup route gives the paths of the two e-reader endpoints (with the prefix of `root_path` or of
a trusted proxy), the clock of the server and what it knows of TLS. It gives no address built from
`Host` and no secret: the page writes the address from the one the browser used.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    NotAllowedError,
    Principal,
    ProfileError,
    UnknownUserError,
)
from shelfhold.interfaces.http_api.auth import gate_of, require_session, reserved_action
from shelfhold.interfaces.http_api.opds.links import mount_prefix
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import client_scheme_of

__all__ = ["DEVICE_TOKENS_PATH", "build_device_tokens_router"]

DEVICE_TOKENS_PATH = "/api/v1/device-tokens"
_ADULTS = ("admin", "member")
# the service writes the audit line, with its target, in the transaction that does the thing
_ISSUE = reserved_action(RESERVED_ACTIONS["device_token.issue"], roles=_ADULTS, audit=False)
_REVOKE = reserved_action(RESERVED_ACTIONS["device_token.manage"], roles=_ADULTS, audit=False)


class DeviceTokenPairIn(BaseModel):
    """`subject_user_id` is the profile the reader reads as: omitted, it is the caller's own (an
    admin has to name one, as an admin cannot be a subject)."""

    model_config = ConfigDict(extra="forbid")
    subject_user_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    name: StrictStr | None = Field(default=None, max_length=80)
    expires_days: StrictInt | None = Field(default=None, ge=1, le=3650)


class DeviceTokenPaired(BaseModel):
    """What is shown once. `secret` is typed as it is: no separator, case kept."""

    public_id: str
    secret: str
    subject_user_id: str
    expires_at: datetime | None


class DeviceTokenOut(BaseModel):
    public_id: str
    name: str | None
    subject_user_id: str
    subject_username: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class DeviceTokenList(BaseModel):
    items: list[DeviceTokenOut]


class DeviceTokenSetup(BaseModel):
    opds_path: str
    kosync_path: str
    server_time: datetime
    https: bool
    plain_http_allowed: bool
    document_matching: str  # what KOReader must be set to: the binary method


def _adult(principal: Annotated[Principal, Depends(require_session)]) -> Principal:
    """A session of an admin or a member: a child pairs nothing, a key or a token no more."""
    if principal.role not in _ADULTS:
        raise ProblemError("forbidden")
    return principal


def build_device_tokens_router() -> APIRouter:
    router = APIRouter(prefix=DEVICE_TOKENS_PATH, tags=["device-tokens"])

    @router.get("/setup", response_model=DeviceTokenSetup)
    def device_token_setup(
        request: Request, _principal: Annotated[Principal, Depends(_adult)]
    ) -> Any:
        prefix = mount_prefix(request.scope)
        return DeviceTokenSetup(
            opds_path=f"{prefix}/opds",
            kosync_path=f"{prefix}/kosync",
            server_time=gate_of(request).auth.now().astimezone(UTC),
            https=client_scheme_of(request.scope) == "https",
            plain_http_allowed=bool(getattr(request.app.state, "device_allow_plain_http", False)),
            document_matching="binary",
        )

    @router.get("", response_model=DeviceTokenList)
    def device_token_list(
        request: Request, principal: Annotated[Principal, Depends(_adult)]
    ) -> Any:
        records = gate_of(request).auth.device_tokens(by=principal)
        return DeviceTokenList(
            items=[
                DeviceTokenOut(
                    public_id=r.public_id,
                    name=r.name,
                    subject_user_id=r.subject.id,
                    subject_username=r.subject.username,
                    created_at=r.created_at,
                    expires_at=r.expires_at,
                    last_used_at=r.last_used_at,
                )
                for r in records
                if r.revoked_at is None
            ]
        )

    @router.post("", status_code=201, response_model=DeviceTokenPaired)
    def device_token_pair(
        request: Request,
        body: DeviceTokenPairIn,
        response: Response,
        principal: Annotated[Principal, Depends(_ISSUE)],
    ) -> Any:
        subject = body.subject_user_id or (
            principal.user_id if principal.role == "member" else None
        )
        if subject is None:
            raise ProblemError("profile_refused", extensions={"reason": "subject_required"})
        try:
            created = gate_of(request).auth.pair_device_token(
                by=principal, subject_user_id=subject, name=body.name,
                expires_days=body.expires_days,
            )  # fmt: skip
        except (
            NotAllowedError,
            UnknownUserError,
        ):  # the same answer for a profile that is not there
            raise ProblemError("forbidden") from None
        except ProfileError as error:
            raise ProblemError("profile_refused", extensions={"reason": error.code}) from None
        response.headers["Cache-Control"] = "no-store"
        return DeviceTokenPaired(
            public_id=created.public_id,
            secret=created.secret,
            subject_user_id=created.subject_user_id,
            expires_at=created.expires_at,
        )

    @router.delete("/{public_id}", status_code=204)
    def device_token_revoke(
        request: Request, public_id: str, principal: Annotated[Principal, Depends(_REVOKE)]
    ) -> Response:
        if not gate_of(request).auth.revoke_device_token(public_id[:32], by=principal):
            raise ProblemError("device_token_not_found")
        return Response(status_code=204)

    return router
