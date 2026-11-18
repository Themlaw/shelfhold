"""OpenID Connect routes (M5 L03, EF-USR-004, EF-USR-024, ADR-0018 amended).

- `GET /api/v1/auth/oidc/login`: the browser is sent to the provider (anonymous). Absent (404)
  unless `SHELFHOLD_OIDC_*` is set.
- `GET /api/v1/auth/oidc/callback`: where the provider sends the browser back. A sign-in opens a
  session; every refusal is the same redirect to the login page, with no cookie of a session.
- `POST /api/v1/auth/oidc/link`: the reserved action `oidc.link_own`, from the account's own fresh
  session (an adult's: a child is refused): answers the address to send the browser to.
- `GET /api/v1/auth/oidc/link/confirm`: where a link comes back to. A page of this site says which
  identity would be linked to which account; nothing is written by the return nor by looking at it.
- `POST /api/v1/auth/oidc/link/confirm`: the confirmation (or the refusal), the same reserved
  action again, from the session that started the flow, with the usual CSRF token.
- `GET /api/v1/auth/oidc/identities`: the links of the caller's own account.
- `PUT /api/v1/profiles/{id}/oidc`: the reserved action `oidc.link_account`, an admin's, for her own
  account or an adult member's (never another admin's, never a child profile).

The flow cookie is `SameSite=Lax`, the one exception to `Strict`: the provider's redirect to the
callback is a navigation from another site, and a `Strict` cookie would not come back with it.
"""

from datetime import datetime
from html import escape
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from shelfhold.i18n import negotiate, t_lang
from shelfhold.identity.public import (
    FLOW_COOKIE_MAX_AGE_S,
    RESERVED_ACTIONS,
    OidcLink,
    OidcRefusedError,
    OidcUnavailableError,
    OidcUnlink,
    Principal,
    ThrottledError,
)
from shelfhold.infrastructure.http_headers import hash_csp
from shelfhold.interfaces.http_api.auth import (
    LOGIN_PAGE,
    OIDC_LOGIN_PATH,
    clear_oidc_cookie,
    gate_of,
    require_session,
    reserved_action,
    set_oidc_cookie,
    set_session_cookies,
)
from shelfhold.interfaces.http_api.login import _client_ip, _fragment, _throttled
from shelfhold.interfaces.http_api.problems import ProblemError

OIDC_PATH = "/api/v1/auth/oidc"
OIDC_CALLBACK_PATH = f"{OIDC_PATH}/callback"
OIDC_LINK_PATH = f"{OIDC_PATH}/link"
OIDC_CONFIRM_PATH = f"{OIDC_LINK_PATH}/confirm"
OIDC_IDENTITIES_PATH = f"{OIDC_PATH}/identities"
OIDC_REAUTH_PATH = f"{OIDC_PATH}/reauth"
# where a person who linked, or proved themselves again, comes back to: the settings screen of
# their provider identities, with the result in the query (`linked`, `reauth`, `refused`)
LINK_RESULT_PATH = "/reglages/compte/oidc"

_ADULTS = ("admin", "member")  # a child profile is never linked
_LINK = reserved_action(RESERVED_ACTIONS["oidc.link_own"], roles=_ADULTS, audit=False)
_LINK_ACCOUNT = reserved_action(
    RESERVED_ACTIONS["oidc.link_account"], roles=("admin",), audit=False
)
_UNLINK = reserved_action(RESERVED_ACTIONS["oidc.unlink_own"], roles=_ADULTS, audit=False)
_UNLINK_ACCOUNT = reserved_action(
    RESERVED_ACTIONS["oidc.unlink_account"], roles=("admin",), audit=False
)


class OidcLinkStart(BaseModel):
    """Where to send the browser: the provider's page, with the flow already begun."""

    authorization_url: str


class OidcSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sub: str = Field(min_length=1, max_length=255)


class OidcConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool


class OidcIdentityItem(BaseModel):
    """A link of the caller's account: the provider and since when, never the identifier."""

    issuer: str
    linked_at: datetime
    # an admin made the link; None when the audit line is gone
    linked_by_other: bool | None


class OidcIdentityList(BaseModel):
    # the provider this instance is set to ask, None when no provider is configured
    configured_issuer: str | None
    items: list[OidcIdentityItem]


_CONFIRM = _fragment("oidc_confirm.html")
CONFIRM_CSP = hash_csp(_CONFIRM.template)


def confirm_page_html(
    *, language: str, issuer: str, sub: str, account: str, csrf_cookie: str
) -> str:
    """Every value is escaped; the page's script and style are inline and fixed (their hashes are
    in the CSP, which has no `unsafe-inline`)."""

    host = urlsplit(issuer).netloc or issuer
    return _CONFIRM.substitute(
        lang=escape(language),
        title=escape(t_lang("oidc_confirm.title", language=language)),
        question=escape(t_lang("oidc_confirm.question", language=language)),
        identity_label=escape(t_lang("oidc_confirm.identity", language=language)),
        account_label=escape(t_lang("oidc_confirm.account", language=language)),
        yes=escape(t_lang("oidc_confirm.yes", language=language)),
        no=escape(t_lang("oidc_confirm.no", language=language)),
        error=escape(t_lang("oidc_confirm.error", language=language)),
        identity=escape(sub),
        provider_label=escape(t_lang("oidc_confirm.provider", language=language)),
        provider=escape(host),
        account=escape(account),
        endpoint=OIDC_CONFIRM_PATH,
        csrf_cookie=escape(csrf_cookie),
        done=f"{LINK_RESULT_PATH}?oidc=linked",
        declined=LINK_RESULT_PATH,
    )


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
                if refused.purpose in ("link", "reauth")
                else _refused_to_login()
            )
            clear_oidc_cookie(response, names)
            response.headers["Cache-Control"] = "no-store"
            return response
        if outcome.pending:  # the flow cookie stays: it is what the confirmation is bound to
            response = RedirectResponse(OIDC_CONFIRM_PATH, status_code=303)
            response.headers["Cache-Control"] = "no-store"
            return response
        if outcome.issued is not None:
            response = RedirectResponse(
                f"{LINK_RESULT_PATH}?oidc=reauth" if outcome.purpose == "reauth" else "/",
                status_code=303,
            )
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

    @router.post(OIDC_REAUTH_PATH, response_model=OidcLinkStart)
    def oidc_reauth(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> Response:
        """The session proves itself again at the provider (`prompt=login`, `max_age`). Not a
        reserved action: it is the proof one asks for. For a session of a linked adult only."""
        gate = gate_of(request)
        if not gate.auth.oidc_available or principal.session_id is None:
            raise ProblemError("oidc_unavailable")
        if principal.role == "child" or principal.via_forward:
            raise ProblemError("forbidden", extensions={"action": "oidc.reauth"})
        try:
            start = gate.auth.oidc.begin_reauth(
                user_id=principal.user_id, session_id=principal.session_id
            )
        except OidcRefusedError:
            raise ProblemError("oidc_not_linked") from None
        except OidcUnavailableError:
            raise ProblemError("oidc_unavailable") from None
        except ThrottledError as error:
            raise _throttled(error) from None
        response = JSONResponse({"authorization_url": start.url})
        set_oidc_cookie(
            response, gate.cookies_for(request.scope), start.binder, max_age=FLOW_COOKIE_MAX_AGE_S
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(OIDC_CONFIRM_PATH, include_in_schema=False)
    def oidc_confirm_page(request: Request) -> Response:
        gate = getattr(request.app.state, "auth_gate", None)
        if gate is None or not gate.auth.oidc_available:
            return Response(status_code=404)
        names = gate.cookies_for(request.scope)
        pending = gate.auth.oidc.pending_link(request.cookies.get(names.oidc, ""))
        if pending is None:
            return RedirectResponse(f"{LINK_RESULT_PATH}?oidc=refused", status_code=303)
        return HTMLResponse(
            confirm_page_html(
                language=negotiate(request.headers.get("accept-language")),
                issuer=pending.issuer,
                sub=pending.sub,
                account=pending.account,
                csrf_cookie=names.csrf,
            ),
            headers={"Content-Security-Policy": CONFIRM_CSP, "Cache-Control": "no-store"},
        )

    @router.post(OIDC_CONFIRM_PATH, status_code=204)
    def oidc_confirm(
        request: Request,
        body: OidcConfirmation,
        principal: Annotated[Principal, Depends(_LINK)],
    ) -> Response:
        gate = gate_of(request)
        names = gate.cookies_for(request.scope)
        if not gate.auth.oidc_available or principal.session_id is None:
            raise ProblemError("oidc_unavailable")
        try:
            outcome = gate.auth.oidc.confirm_link(
                binder=request.cookies.get(names.oidc, ""),
                user_id=principal.user_id,
                session_id=principal.session_id,
                accept=body.confirm,
            )
        except OidcRefusedError:
            raise ProblemError("oidc_link_expired") from None
        answer = Response(status_code=204)
        clear_oidc_cookie(answer, names)
        answer.headers["Cache-Control"] = "no-store"
        if outcome is None or outcome in (OidcLink.LINKED, OidcLink.UNCHANGED):
            return answer
        if outcome in (OidcLink.SUBJECT_TAKEN, OidcLink.ACCOUNT_HAS_ANOTHER):
            raise ProblemError("oidc_conflict", extensions={"reason": outcome.value})
        if outcome is OidcLink.CHILD:
            raise ProblemError("oidc_child_profile")
        raise ProblemError("oidc_link_expired")

    @router.get(OIDC_IDENTITIES_PATH, response_model=OidcIdentityList)
    def oidc_identities(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> OidcIdentityList:
        auth = gate_of(request).auth
        views = auth.oidc_identities_of(principal)
        return OidcIdentityList(
            configured_issuer=auth.oidc.issuer if auth.oidc_available else None,
            items=[
                OidcIdentityItem(
                    issuer=v.issuer, linked_at=v.linked_at, linked_by_other=v.linked_by_other
                )
                for v in views
            ]
        )

    def _unlinked(outcome: OidcUnlink) -> Response:
        if outcome is OidcUnlink.UNLINKED:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        if outcome is OidcUnlink.ADMIN_TARGET:
            raise ProblemError("oidc_admin_target")
        if outcome is OidcUnlink.NO_ACCOUNT:
            raise ProblemError("profile_not_found")
        raise ProblemError("oidc_not_linked")

    @router.delete(OIDC_IDENTITIES_PATH, status_code=204)
    def oidc_unlink(
        request: Request, principal: Annotated[Principal, Depends(_UNLINK)]
    ) -> Response:
        """The reserved action `oidc.unlink_own`: a fresh session (for one the provider opened,
        proved again there), and the holder is told."""
        return _unlinked(
            gate_of(request).auth.unlink_oidc_identities(user_id=principal.user_id, by=principal)
        )

    @router.delete("/api/v1/profiles/{profile_id}/oidc", status_code=204, tags=["profiles"])
    def oidc_unlink_account(
        request: Request,
        profile_id: str,
        principal: Annotated[Principal, Depends(_UNLINK_ACCOUNT)],
    ) -> Response:
        return _unlinked(
            gate_of(request).auth.unlink_oidc_identities(user_id=profile_id[:26], by=principal)
        )

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
        if outcome is OidcLink.ADMIN_TARGET:
            raise ProblemError("oidc_admin_target")
        if outcome in (OidcLink.SUBJECT_TAKEN, OidcLink.ACCOUNT_HAS_ANOTHER):
            raise ProblemError("oidc_conflict", extensions={"reason": outcome.value})
        return Response(status_code=204)

    return router
