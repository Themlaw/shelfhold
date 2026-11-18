"""OpenID Connect routes (M5 L03, EF-USR-004, EF-USR-024, ADR-0018 amended).

- `GET /api/v1/auth/oidc/login`: the browser is sent to the provider (anonymous). Absent (404)
  unless `SHELFHOLD_OIDC_*` is set.
- `GET /api/v1/auth/oidc/callback`: where the provider sends the browser back. A sign-in opens a
  session; every refusal is the same redirect to the login page, with no cookie of a session.
- `POST /api/v1/auth/oidc/link`: the reserved action `oidc.link_own`, from the account's own fresh
  session (an adult's: a child is refused): answers the address to send the browser to.
- `PUT /api/v1/profiles/{id}/oidc`: the reserved action `oidc.link_account`, an admin's, for an
  account that already exists (a child profile is refused).

The flow cookie is `SameSite=Lax`, the one exception to `Strict`: the provider's redirect to the
callback is a navigation from another site, and a `Strict` cookie would not come back with it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.identity.public import (
    FLOW_COOKIE_MAX_AGE_S,
    RESERVED_ACTIONS,
    OidcLink,
    OidcRefusedError,
    OidcUnavailableError,
    Principal,
    ThrottledError,
)
from shelfhold.interfaces.http_api.auth import (
    LOGIN_PAGE,
    OIDC_LOGIN_PATH,
    clear_oidc_cookie,
    gate_of,
    reserved_action,
    set_oidc_cookie,
    set_session_cookies,
)
from shelfhold.interfaces.http_api.login import _client_ip, _throttled
from shelfhold.interfaces.http_api.problems import ProblemError

OIDC_PATH = "/api/v1/auth/oidc"
OIDC_CALLBACK_PATH = f"{OIDC_PATH}/callback"
OIDC_LINK_PATH = f"{OIDC_PATH}/link"
# where a person who linked from their account goes back to: the home screen, with the result in
# the query (the settings screen of L04 takes it over)
LINK_RESULT_PATH = "/"

_ADULTS = ("admin", "member")  # a child profile is never linked
_LINK = reserved_action(RESERVED_ACTIONS["oidc.link_own"], roles=_ADULTS, audit=False)
_LINK_ACCOUNT = reserved_action(
    RESERVED_ACTIONS["oidc.link_account"], roles=("admin",), audit=False
)


class OidcLinkStart(BaseModel):
    """Where to send the browser: the provider's page, with the flow already begun."""

    authorization_url: str


class OidcSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub: str = Field(min_length=1, max_length=255)


def _refused_to_login() -> RedirectResponse:
    return RedirectResponse(f"{LOGIN_PAGE}?oidc_error=1", status_code=303)


def build_oidc_router() -> APIRouter:
    router = APIRouter(tags=["auth"])

    @router.get(OIDC_LOGIN_PATH, include_in_schema=False)
    def oidc_login(request: Request) -> Response:
        gate = getattr(request.app.state, "auth_gate", None)
        if gate is None or not gate.auth.oidc_available:
            return Response(status_code=404)  # the function is absent, not forbidden
        try:
            start = gate.auth.oidc.begin_login(ip=_client_ip(request))
        except ThrottledError as error:
            raise _throttled(error) from None
        except OidcUnavailableError:
            return _refused_to_login()
        response = RedirectResponse(start.url, status_code=302)
        set_oidc_cookie(
            response, gate.cookies_for(request.scope), start.binder, max_age=FLOW_COOKIE_MAX_AGE_S
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(OIDC_CALLBACK_PATH, include_in_schema=False)
    def oidc_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        gate = getattr(request.app.state, "auth_gate", None)
        if gate is None or not gate.auth.oidc_available:
            return Response(status_code=404)
        names = gate.cookies_for(request.scope)
        try:
            outcome = gate.auth.oidc.finish(
                state=(state or "")[:512],
                code="" if error else (code or "")[:2048],
                binder=request.cookies.get(names.oidc, ""),
                ip=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                presented_token=request.cookies.get(names.session),
            )
        except ThrottledError as throttled:
            raise _throttled(throttled) from None
        except OidcRefusedError as refused:
            response = (
                RedirectResponse(f"{LINK_RESULT_PATH}?oidc=refused", status_code=303)
                if refused.purpose == "link"
                else _refused_to_login()
            )
            clear_oidc_cookie(response, names)
            response.headers["Cache-Control"] = "no-store"
            return response
        if outcome.issued is not None:
            response = RedirectResponse("/", status_code=303)
            set_session_cookies(response, names, outcome.issued)
        else:
            response = RedirectResponse(f"{LINK_RESULT_PATH}?oidc=linked", status_code=303)
        clear_oidc_cookie(response, names)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post(OIDC_LINK_PATH, response_model=OidcLinkStart)
    def oidc_link(request: Request, principal: Annotated[Principal, Depends(_LINK)]) -> Response:
        gate = gate_of(request)
        if not gate.auth.oidc_available or principal.session_id is None:
            raise ProblemError("oidc_unavailable")
        try:
            start = gate.auth.oidc.begin_link(
                user_id=principal.user_id, session_id=principal.session_id
            )
        except OidcUnavailableError:
            raise ProblemError("oidc_unavailable") from None
        response = JSONResponse({"authorization_url": start.url})
        set_oidc_cookie(
            response, gate.cookies_for(request.scope), start.binder, max_age=FLOW_COOKIE_MAX_AGE_S
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.put("/api/v1/profiles/{profile_id}/oidc", status_code=204, tags=["profiles"])
    def link_account(
        request: Request,
        profile_id: str,
        body: OidcSubject,
        principal: Annotated[Principal, Depends(_LINK_ACCOUNT)],
    ) -> Response:
        auth = gate_of(request).auth
        if not auth.oidc_available:
            raise ProblemError("oidc_unavailable")
        outcome = auth.link_oidc_identity(
            user_id=profile_id[:26], issuer=auth.oidc.issuer, sub=body.sub, by=principal
        )
        if outcome is OidcLink.NO_ACCOUNT:
            raise ProblemError("profile_not_found")
        if outcome is OidcLink.CHILD:
            raise ProblemError("oidc_child_profile")
        if outcome in (OidcLink.SUBJECT_TAKEN, OidcLink.ACCOUNT_HAS_ANOTHER):
            raise ProblemError("oidc_conflict", extensions={"reason": outcome.value})
        return Response(status_code=204)

    return router
