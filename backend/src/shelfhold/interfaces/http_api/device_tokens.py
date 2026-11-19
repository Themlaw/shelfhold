"""`/api/v1/device-tokens` and `/api/v1/devices`: pairing an e-reader with a profile and the
registry of what was paired (M4 L46b, M5 L02, EF-UX-004, EF-USR-008, EF-USR-022 (4), ADR-0053 §4,
D19). The routes behind the screens "Connect a device" and "Devices".

Creating a token is the reserved action `device_token.issue`: a fresh session in every profile,
never an API key, never a device token (a token is read by `/opds` and `/kosync` only). Revoking is
`device_token.manage`. A member pairs and revokes their own tokens, an admin pairs for a member or
a child (an admin is never the subject of a token) and revokes any, a child manages none. The
secret is in the answer of the creation, once, with `Cache-Control: no-store`; it is not in a URL,
not in a list, not in the audit, and the reserved guard keeps the answer out of the replay store
of `Idempotency-Key`. A token of another profile answers like one that is not there.

A pairing makes one device (a label, a kind, when it was last seen). The registry lists the live
devices of a profile (a member's own, an admin's all, none for a child), rotates a device's secret
(`device_token.rotate`: a new secret on the same device, shown once, the old one refused from the
next request, nothing to pair again) and removes a device (`device.remove`: every token it holds
ends). A device of another profile answers like one that is not there.

The setup route gives the paths of the two e-reader endpoints (with the prefix of `root_path` or of
a trusted proxy), the clock of the server and what it knows of TLS. It gives no address built from
`Host` and no secret: the page writes the address from the one the browser used.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    DeviceRecord,
    NotAllowedError,
    Principal,
    ProfileError,
    UnknownUserError,
)
from shelfhold.interfaces.http_api.auth import gate_of, require_session, reserved_action
from shelfhold.interfaces.http_api.opds.links import mount_prefix
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import client_scheme_of

__all__ = [
    "DEVICES_PATH",
    "DEVICE_TOKENS_PATH",
    "build_device_tokens_router",
    "build_devices_router",
]

DEVICE_TOKENS_PATH = "/api/v1/device-tokens"
DEVICES_PATH = "/api/v1/devices"
DeviceKind = Literal["koreader", "kobo", "opds", "browser", "other"]  # `DEVICE_KINDS`, as a type
_ADULTS = ("admin", "member")
# the service writes the audit line, with its target, in the transaction that does the thing
_ISSUE = reserved_action(RESERVED_ACTIONS["device_token.issue"], roles=_ADULTS, audit=False)
_REVOKE = reserved_action(RESERVED_ACTIONS["device_token.manage"], roles=_ADULTS, audit=False)
_ROTATE = reserved_action(RESERVED_ACTIONS["device_token.rotate"], roles=_ADULTS, audit=False)
_REMOVE = reserved_action(RESERVED_ACTIONS["device.remove"], roles=_ADULTS, audit=False)


class DeviceTokenPairIn(BaseModel):
    """`subject_user_id` is the profile the reader reads as: omitted, it is the caller's own (an
    admin has to name one, as an admin cannot be a subject)."""

    model_config = ConfigDict(extra="forbid")
    subject_user_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    name: StrictStr | None = Field(default=None, max_length=80)
    kind: DeviceKind | None = None
    expires_days: StrictInt | None = Field(default=None, ge=1, le=3650)


class DeviceTokenPaired(BaseModel):
    """What is shown once. `secret` is typed as it is: no separator, case kept."""

    public_id: str
    secret: str
    subject_user_id: str
    expires_at: datetime | None
    device_id: str


class DeviceRotateIn(BaseModel):
    """Only a token past its expiry needs `expires_days`: a new secret would be refused as it is."""

    model_config = ConfigDict(extra="forbid")
    expires_days: StrictInt | None = Field(default=None, ge=1, le=3650)


class DeviceOut(BaseModel):
    """What the registry shows of a device: never the secret, never what verifies it."""

    id: str
    label: str | None
    kind: DeviceKind
    subject_user_id: str
    subject_username: str
    public_id: str | None
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class DeviceList(BaseModel):
    items: list[DeviceOut]


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
                expires_days=body.expires_days, kind=body.kind,
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
            device_id=created.device_id,
        )

    @router.delete("/{public_id}", status_code=204)
    def device_token_revoke(
        request: Request, public_id: str, principal: Annotated[Principal, Depends(_REVOKE)]
    ) -> Response:
        if not gate_of(request).auth.revoke_device_token(public_id[:32], by=principal):
            raise ProblemError("device_token_not_found")
        return Response(status_code=204)

    return router


def _device_out(record: DeviceRecord) -> DeviceOut:
    device = record.device
    return DeviceOut(
        id=device.id,
        label=device.label,
        kind=device.kind,  # type: ignore[arg-type]
        subject_user_id=device.subject_user_id,
        subject_username=record.subject.username,
        public_id=record.public_id,
        created_at=device.created_at,
        last_seen_at=device.last_seen_at,
        expires_at=record.expires_at,
        revoked_at=device.revoked_at,
    )


def build_devices_router() -> APIRouter:
    router = APIRouter(prefix=DEVICES_PATH, tags=["devices"])

    @router.get("", response_model=DeviceList)
    def device_list(
        request: Request,
        principal: Annotated[Principal, Depends(_adult)],
        subject_user_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    ) -> Any:
        try:
            records = gate_of(request).auth.devices(by=principal, subject_user_id=subject_user_id)
        except NotAllowedError:  # another profile's list: the same answer as a profile not there
            raise ProblemError("forbidden") from None
        return DeviceList(items=[_device_out(r) for r in records])

    @router.post("/{device_id}/rotate", response_model=DeviceTokenPaired)
    def device_rotate(
        request: Request,
        device_id: str,
        response: Response,
        principal: Annotated[Principal, Depends(_ROTATE)],
        body: DeviceRotateIn | None = None,
    ) -> Any:
        try:
            rotated = gate_of(request).auth.rotate_device(
                device_id, by=principal, expires_days=body.expires_days if body else None
            )
        except NotAllowedError:
            raise ProblemError("forbidden") from None
        except ProfileError as error:
            if error.code == "device_rotated_meanwhile":  # another rotation won: nothing was shown
                raise ProblemError("device_rotation_conflict") from None
            raise ProblemError("profile_refused", extensions={"reason": error.code}) from None
        if rotated is None:
            raise ProblemError("device_token_not_found")
        response.headers["Cache-Control"] = "no-store"
        return DeviceTokenPaired(
            public_id=rotated.public_id,
            secret=rotated.secret,
            subject_user_id=rotated.subject_user_id,
            expires_at=rotated.expires_at,
            device_id=rotated.device_id,
        )

    @router.delete("/{device_id}", status_code=204)
    def device_remove(
        request: Request, device_id: str, principal: Annotated[Principal, Depends(_REMOVE)]
    ) -> Response:
        try:
            removed = gate_of(request).auth.retire_device(device_id, by=principal)
        except NotAllowedError:
            raise ProblemError("forbidden") from None
        if not removed:
            raise ProblemError("device_token_not_found")
        return Response(status_code=204)

    return router
