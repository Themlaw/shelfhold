"""Neutral login page and the session routes (EF-USR-021, ENF-PRIV-002, ENF-LEG-012).

The page names no tool, carries no Open Graph or Twitter tag, no image, and offers the source
code of the running version (AGPL section 13). Its style and its scripts are files of this origin
(`page_assets`): the CSP has no `unsafe-inline`. Without JavaScript the form posts to `/login`,
which refuses it: the password never travels in a URL.
"""

from datetime import datetime
from functools import cache
from html import escape
from importlib import resources
from string import Template
from typing import Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from shelfhold.i18n import negotiate, t_lang
from shelfhold.identity.public import (
    MAX_PASSWORD_LENGTH,
    InvalidCredentialsError,
    PasswordLoginSuspendedError,
    Principal,
    RememberNeedsConfirmationError,
    ThrottledError,
    UnknownReauthMethodError,
)
from shelfhold.infrastructure.http_headers import page_csp
from shelfhold.interfaces.http_api.auth import (
    LOGIN_PAGE,
    LOGIN_PATH,
    OIDC_LOGIN_PATH,
    PASSKEY_LOGIN_OPTIONS_PATH,
    PASSKEY_LOGIN_PATH,
    PASSKEY_SCRIPT_PATH,
    clear_session_cookies,
    gate_of,
    require_principal,
    require_session,
    set_session_cookies,
)
from shelfhold.interfaces.http_api.page_assets import (
    APPEARANCE_PATH,
    LOGIN_SCRIPT_PATH,
    STYLE_PATH,
)
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import client_ip_of

_TEMPLATE = Template(
    resources.files("shelfhold.interfaces.http_api").joinpath("pages/login.html").read_text("utf-8")
)


@cache
def passkey_script() -> str:
    return (
        resources.files("shelfhold.interfaces.http_api")
        .joinpath("static/passkey.js")
        .read_text("utf-8")
    )


def _fragment(name: str) -> Template:
    return Template(
        resources.files("shelfhold.interfaces.http_api")
        .joinpath(f"pages/{name}")
        .read_text("utf-8")
    )


def safe_next(raw: str | None) -> str:
    """Where a successful sign-in sends the person back to: only a relative path of this site (one
    leading `/`, not `//`, no scheme, no backslash, no control character, and the same after one
    decoding, since a browser or a proxy may decode once); anything else is `/`."""
    if not raw or len(raw) > 2048:
        return "/"
    for candidate in (raw, unquote(raw)):
        if (
            not candidate.startswith("/")
            or candidate.startswith("//")
            or "\\" in candidate
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate)
            or urlsplit(candidate).scheme
            or urlsplit(candidate).netloc
        ):
            return "/"
    return raw


LITE_SIGN_IN = "/lite/sign-in"  # the e-readers' own door (no script there)
_BUTTON = _fragment("passkey_button.html")
_OIDC_BUTTON = _fragment("oidc_button.html")
_SCRIPT_TAG = _fragment("passkey_script.html")
_ALTERNATIVES = _fragment("alternatives.html")
# The page loads its style and its scripts from its own origin, and nothing inline.
LOGIN_CSP = page_csp(scripts=True)
PASSKEY_LOGIN_CSP = LOGIN_CSP  # the passkey script is one more file of the same origin


def login_page_html(
    source_url: str,
    *,
    passkeys: bool = False,
    language: str = "fr",
    oidc: bool = False,
    oidc_failed: bool = False,
    provider: str | None = None,
    next_path: str = "/",
) -> str:
    """`passkeys`: the button and the same-origin script are there only in a secure context.
    `language`: the language of the request (ENF-I18N-001); the page is anonymous, so the
    `Accept-Language` of the browser picks it.
    `oidc`: the sign-in link of the identity provider (M5 L03), there only when it is configured;
    `provider`: the host of the configured provider, named on its button;
    `next_path`: the path to go back to after the sign-in (already checked by `safe_next`);
    `oidc_failed`: a sign-in that came back refused, said in one neutral line."""
    button = script = ""
    if passkeys:
        button = _BUTTON.substitute(
            options=PASSKEY_LOGIN_OPTIONS_PATH,
            login=PASSKEY_LOGIN_PATH,
            error=escape(t_lang("login.error", language=language)),
            label=escape(t_lang("login.passkey", language=language)),
            next=escape(next_path),
        )
        script = _SCRIPT_TAG.substitute(src=PASSKEY_SCRIPT_PATH)
    values = {
        "lang": language,
        "title": t_lang("login.title", language=language),
        "lede": t_lang("login.lede", language=language),
        "username": t_lang("login.username", language=language),
        "password": t_lang("login.password", language=language),
        "remember": t_lang("login.remember", language=language),
        "remember_hint": t_lang("login.remember_hint", language=language),
        "help": t_lang("login.help", language=language),
        "forgot": t_lang("login.forgot", language=language),
        "show_password": t_lang("login.show_password", language=language),
        "hide_password": t_lang("login.hide_password", language=language),
        "next": next_path,
        "lite": t_lang("login.lite", language=language),
        "lite_href": LITE_SIGN_IN,
        "style": STYLE_PATH,
        "appearance": APPEARANCE_PATH,
        "login_script": LOGIN_SCRIPT_PATH,
        "submit": t_lang("login.submit", language=language),
        "error": t_lang("login.error", language=language),
        "source": t_lang("login.source", language=language),
        "source_url": source_url,
        "endpoint": LOGIN_PATH,
        "max_password": str(MAX_PASSWORD_LENGTH),
    }
    escaped = {name: escape(value) for name, value in values.items()}
    oidc_button = (
        _OIDC_BUTTON.substitute(
            href=OIDC_LOGIN_PATH,
            label=escape(
                t_lang("login.oidc_named", language=language, provider=provider)
                if provider
                else t_lang("login.oidc", language=language)
            ),
        )
        if oidc
        else ""
    )
    notice = escape(t_lang("login.oidc_error", language=language)) if oidc_failed else ""
    alternatives = (
        _ALTERNATIVES.substitute(
            {
                "or": escape(t_lang("login.or", language=language)),
                "passkey": button,
                "oidc": oidc_button,
            }
        )
        if button or oidc_button
        else ""
    )
    return _TEMPLATE.substitute(
        {
            **escaped,
            "alternatives": alternatives,
            "passkey_script": script,
            "notice": notice,
            "message_state": "" if notice else "hidden",
        }
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    remember: bool = True  # P2: ticked by default
    confirm_remember: bool = (
        False  # the hardened profile refuses `remember` without it (EF-USR-022)
    )


class CsrfToken(BaseModel):
    csrf_token: str


class PasswordReauth(BaseModel):
    method: Literal["password"]
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class PasskeyReauth(BaseModel):
    """The assertion answers a challenge from `/api/v1/auth/passkeys/reauth/options`."""

    method: Literal["passkey"]
    flow: str = Field(min_length=1, max_length=256)
    credential: dict[str, Any]


ReauthRequest = Annotated[PasswordReauth | PasskeyReauth, Field(discriminator="method")]


class ReauthResponse(BaseModel):
    csrf_token: str
    auth_time: datetime


class SessionInfo(BaseModel):
    user_id: str
    username: str
    role: str
    credential: str
    auth_time: datetime | None = None


def _client_ip(request: Request) -> str:
    # The real address (ENF-SEC-008, D15): the socket peer, or the caller behind a trusted proxy.
    # `serve` runs uvicorn with `proxy_headers=False`: `X-Forwarded-For` is read only by
    # `ClientIpMiddleware`, and only when the peer is inside `http_trusted_proxies`.
    return client_ip_of(request.scope)


def _throttled(error: ThrottledError) -> ProblemError:
    return ProblemError(
        "too_many_attempts",
        extensions={"retry_after_s": error.retry_after_s},
        headers={"Retry-After": str(error.retry_after_s)},
    )


def build_login_router(source_url: str) -> APIRouter:
    router = APIRouter()
    page = cache(
        lambda passkeys, language, oidc, failed, provider: login_page_html(
            source_url,
            passkeys=passkeys,
            language=language,
            oidc=oidc,
            oidc_failed=failed,
            provider=provider,
        )
    )

    @router.get(LOGIN_PAGE, include_in_schema=False)
    def login_page(request: Request) -> HTMLResponse:
        gate = getattr(request.app.state, "auth_gate", None)
        passkeys = gate is not None and gate.auth.passkeys_available
        language = negotiate(request.headers.get("accept-language"))
        oidc = gate is not None and gate.auth.oidc_available
        failed = oidc and request.query_params.get("oidc_error") == "1"
        provider = None
        if gate is not None and oidc:
            issuer = gate.auth.oidc.issuer
            provider = urlsplit(issuer).netloc or issuer
        target = safe_next(request.query_params.get("next"))
        # the page of a return path is not kept (the path comes from the request)
        body = (
            page(passkeys, language, oidc, failed, provider)
            if target == "/"
            else login_page_html(
                source_url,
                passkeys=passkeys,
                language=language,
                oidc=oidc,
                oidc_failed=failed,
                provider=provider,
                next_path=target,
            )
        )
        return HTMLResponse(
            body,
            headers={
                "Content-Security-Policy": PASSKEY_LOGIN_CSP if passkeys else LOGIN_CSP,
                "Cache-Control": "no-store",
            },
        )

    @router.post(LOGIN_PATH, response_model=CsrfToken)
    def login(body: LoginRequest, request: Request) -> Response:
        gate = gate_of(request)
        names = gate.cookies_for(request.scope)
        try:
            issued = gate.auth.login(
                body.username,
                body.password,
                ip=_client_ip(request),
                remember=body.remember,
                confirm_remember=body.confirm_remember,
                presented_token=request.cookies.get(names.session),
                device_proof=request.cookies.get(names.device),
                user_agent=request.headers.get("user-agent"),
            )
        except ThrottledError as error:
            raise _throttled(error) from None
        except InvalidCredentialsError:
            raise ProblemError("invalid_credentials") from None
        except PasswordLoginSuspendedError:
            raise ProblemError("password_login_suspended") from None
        except RememberNeedsConfirmationError:
            raise ProblemError("remember_needs_confirmation") from None
        response = JSONResponse({"csrf_token": issued.csrf_secret})
        set_session_cookies(response, names, issued)
        return response

    @router.post("/api/v1/auth/logout", status_code=204)
    def logout(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> Response:
        gate = gate_of(request)
        gate.auth.logout(principal)
        response = Response(status_code=204)
        clear_session_cookies(response, gate.cookies_for(request.scope))
        return response

    @router.post("/api/v1/auth/reauth", response_model=ReauthResponse)
    def reauth(
        body: ReauthRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Response:
        gate = gate_of(request)
        try:
            issued = gate.auth.reauthenticate(
                principal, body.method, body.model_dump(), ip=_client_ip(request)
            )
        except ThrottledError as error:
            raise _throttled(error) from None
        except (InvalidCredentialsError, UnknownReauthMethodError):
            raise ProblemError("invalid_credentials") from None
        auth_time = issued.principal.auth_time
        response = JSONResponse(
            {
                "csrf_token": issued.csrf_secret,
                "auth_time": auth_time.isoformat() if auth_time else None,
            }
        )
        set_session_cookies(response, gate.cookies_for(request.scope), issued)
        return response

    @router.get("/api/v1/auth/session", response_model=SessionInfo)
    def session(principal: Annotated[Principal, Depends(require_principal)]) -> SessionInfo:
        return SessionInfo(
            user_id=principal.user_id,
            username=principal.username,
            role=principal.role,
            credential=principal.credential.value,
            auth_time=principal.auth_time,
        )

    return router
