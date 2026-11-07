"""Passkey routes (EF-USR-025, ADR-0050): register, list, revoke, sign in, re-authenticate.

Every route answers 409 `passkey_unavailable` outside a secure context (HTTPS or `localhost`
with a `SHELFHOLD_PUBLIC_URL`). The three anonymous routes (the script, the sign-in options and
the sign-in) take JSON only, like the password login, so that no cross-site form reaches them.
The sign-in refuses everything with the answer of a wrong password.
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from shelfhold.i18n import t
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    InvalidCredentialsError,
    LastLoginFactorError,
    PasskeyRecord,
    PasskeyRefusedError,
    PasskeyUnavailableError,
    Principal,
    RememberNeedsConfirmationError,
    ThrottledError,
    UnknownPasskeyError,
)
from shelfhold.interfaces.http_api.auth import (
    PASSKEY_LOGIN_OPTIONS_PATH,
    PASSKEY_LOGIN_PATH,
    PASSKEY_SCRIPT_PATH,
    gate_of,
    require_session,
    reserved_action,
    set_session_cookies,
)
from shelfhold.interfaces.http_api.login import CsrfToken, _client_ip, _throttled, passkey_script
from shelfhold.interfaces.http_api.problems import ProblemError

# Fresh proof in the hardened profile only (P3, review PR #95); API keys never.
_REGISTER = reserved_action(RESERVED_ACTIONS["passkey.register"])
_REVOKE = reserved_action(RESERVED_ACTIONS["passkey.revoke"])
PASSKEYS_PATH = "/api/v1/auth/passkeys"
PASSKEY_REAUTH_OPTIONS_PATH = f"{PASSKEYS_PATH}/reauth/options"


class PasskeyItem(BaseModel):
    id: str
    name: str
    created_at: datetime
    last_used_at: datetime | None
    transports: list[str]


class PasskeyList(BaseModel):
    available: bool
    reason: str | None = None  # why it is not (HTTPS or localhost needed), in words
    rp_id: str | None = None
    items: list[PasskeyItem]


class PasskeyFlow(BaseModel):
    """A challenge and the browser options for `navigator.credentials` (base64url fields)."""

    flow: str
    options: dict[str, Any]


class PasskeyRegistration(BaseModel):
    flow: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=200)
    credential: dict[str, Any]


class PasskeyLoginRequest(BaseModel):
    flow: str = Field(min_length=1, max_length=256)
    credential: dict[str, Any]
    remember: bool = True
    confirm_remember: bool = False


def _item(record: PasskeyRecord) -> PasskeyItem:
    return PasskeyItem(
        id=record.id,
        name=record.name,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        transports=list(record.transports),
    )


def _why(reason: str) -> str:
    messages = {
        "no_public_url": t("passkeys.unavailable.no_public_url"),
        "invalid_url": t("passkeys.unavailable.invalid_url"),
        "insecure_context": t("passkeys.unavailable.insecure_context"),
        "ip_host": t("passkeys.unavailable.ip_host"),
    }
    return messages.get(reason, messages["no_public_url"])


def _unavailable(error: PasskeyUnavailableError) -> ProblemError:
    return ProblemError("passkey_unavailable", detail=_why(error.reason))


def build_passkeys_router() -> APIRouter:
    router = APIRouter()

    @router.get(PASSKEY_SCRIPT_PATH, include_in_schema=False)
    def passkey_script_file(request: Request) -> Response:
        gate = getattr(request.app.state, "auth_gate", None)
        if gate is None or not gate.auth.passkeys_available:
            return Response(status_code=404)  # the function is absent, not forbidden
        return Response(
            passkey_script(),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get(PASSKEYS_PATH, response_model=PasskeyList)
    def passkeys_list(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> PasskeyList:
        auth = gate_of(request).auth
        status = auth.passkey_status()
        reason = _why(status.reason) if status.reason else None
        return PasskeyList(
            available=status.available,
            reason=reason,
            rp_id=status.rp_id,
            items=[_item(p) for p in auth.passkeys_of(principal)],
        )

    @router.post(f"{PASSKEYS_PATH}/options", response_model=PasskeyFlow)
    def passkey_registration_options(
        request: Request, principal: Annotated[Principal, Depends(_REGISTER)]
    ) -> JSONResponse:
        try:
            issued = gate_of(request).auth.passkey_registration_options(principal)
        except PasskeyUnavailableError as error:
            raise _unavailable(error) from None
        except PasskeyRefusedError:
            raise ProblemError("passkey_refused") from None
        return JSONResponse(
            {"flow": issued.flow, "options": issued.options}, headers={"Cache-Control": "no-store"}
        )

    @router.post(PASSKEYS_PATH, response_model=PasskeyItem, status_code=201)
    def passkey_register(
        body: PasskeyRegistration,
        request: Request,
        principal: Annotated[Principal, Depends(_REGISTER)],
    ) -> PasskeyItem:
        try:
            record = gate_of(request).auth.passkey_register(
                principal, body.flow, body.credential, body.name
            )
        except PasskeyUnavailableError as error:
            raise _unavailable(error) from None
        except PasskeyRefusedError:
            raise ProblemError("passkey_refused") from None
        return _item(record)

    @router.delete(f"{PASSKEYS_PATH}/{{passkey_id}}", status_code=204)
    def passkey_revoke(
        passkey_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(_REVOKE)],
    ) -> Response:
        try:
            gate_of(request).auth.passkey_revoke(principal, passkey_id)
        except LastLoginFactorError:
            raise ProblemError("last_login_factor") from None
        except UnknownPasskeyError:
            raise ProblemError("passkey_not_found") from None
        return Response(status_code=204)

    @router.post(PASSKEY_REAUTH_OPTIONS_PATH, response_model=PasskeyFlow)
    def passkey_reauth_options(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> JSONResponse:
        try:
            issued = gate_of(request).auth.passkey_reauth_options(principal)
        except PasskeyUnavailableError as error:
            raise _unavailable(error) from None
        return JSONResponse(
            {"flow": issued.flow, "options": issued.options}, headers={"Cache-Control": "no-store"}
        )

    @router.post(PASSKEY_LOGIN_OPTIONS_PATH, response_model=PasskeyFlow)
    def passkey_login_options(request: Request) -> JSONResponse:
        try:
            issued = gate_of(request).auth.passkey_login_options(ip=_client_ip(request))
        except PasskeyUnavailableError as error:
            raise _unavailable(error) from None
        return JSONResponse(
            {"flow": issued.flow, "options": issued.options}, headers={"Cache-Control": "no-store"}
        )

    @router.post(PASSKEY_LOGIN_PATH, response_model=CsrfToken)
    def passkey_login(body: PasskeyLoginRequest, request: Request) -> Response:
        gate = gate_of(request)
        names = gate.cookies_for(request.scope)
        try:
            issued = gate.auth.passkey_login(
                body.flow,
                body.credential,
                ip=_client_ip(request),
                remember=body.remember,
                confirm_remember=body.confirm_remember,
                presented_token=request.cookies.get(names.session),
            )
        except PasskeyUnavailableError as error:
            raise _unavailable(error) from None
        except ThrottledError as error:
            raise _throttled(error) from None
        except InvalidCredentialsError:
            raise ProblemError("invalid_credentials") from None
        except RememberNeedsConfirmationError:
            raise ProblemError("remember_needs_confirmation") from None
        response = JSONResponse({"csrf_token": issued.csrf_secret})
        set_session_cookies(response, names, issued)
        return response

    return router
