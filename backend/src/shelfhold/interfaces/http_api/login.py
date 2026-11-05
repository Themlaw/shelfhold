"""Neutral login page and the session routes (EF-USR-021, ENF-PRIV-002, ENF-LEG-012).

The page names no tool, carries no Open Graph or Twitter tag, no image, and offers the source
code of the running version (AGPL section 13). Its only script and style are inline, allowed by
their hash: the CSP has no `unsafe-inline`. Without JavaScript the form posts to `/login`, which
refuses it: the password never travels in a URL.
"""

from datetime import datetime
from html import escape
from importlib import resources
from string import Template
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from shelfhold.i18n import t
from shelfhold.identity.public import (
    MAX_PASSWORD_LENGTH,
    InvalidCredentialsError,
    Principal,
    ThrottledError,
    UnknownReauthMethodError,
)
from shelfhold.infrastructure.http_headers import hash_csp
from shelfhold.interfaces.http_api.auth import (
    LOGIN_PAGE,
    LOGIN_PATH,
    clear_session_cookies,
    gate_of,
    require_principal,
    require_session,
    set_session_cookies,
)
from shelfhold.interfaces.http_api.problems import ProblemError

_TEMPLATE = Template(
    resources.files("shelfhold.interfaces.http_api").joinpath("pages/login.html").read_text("utf-8")
)
LOGIN_CSP = hash_csp(_TEMPLATE.template)


def login_page_html(source_url: str) -> str:
    """Only `$` placeholders outside the script and the style: their hashes never change."""
    values = {
        "title": t("login.title"),
        "username": t("login.username"),
        "password": t("login.password"),
        "remember": t("login.remember"),
        "submit": t("login.submit"),
        "error": t("login.error"),
        "source": t("login.source"),
        "source_url": source_url,
        "endpoint": LOGIN_PATH,
        "max_password": str(MAX_PASSWORD_LENGTH),
    }
    return _TEMPLATE.substitute({name: escape(value) for name, value in values.items()})


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    remember: bool = True  # P2: ticked by default


class CsrfToken(BaseModel):
    csrf_token: str


class PasswordReauth(BaseModel):
    method: Literal["password"]
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


# A union discriminated by `method` when passkeys arrive (M1b, EF-USR-025).
ReauthRequest = PasswordReauth


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
    # The socket peer only: X-Forwarded-For is trusted from a configured proxy in M3 alone.
    return request.client.host if request.client else "unknown"


def _throttled(error: ThrottledError) -> ProblemError:
    return ProblemError(
        "too_many_attempts",
        extensions={"retry_after_s": error.retry_after_s},
        headers={"Retry-After": str(error.retry_after_s)},
    )


def build_login_router(source_url: str) -> APIRouter:
    router = APIRouter()
    page = login_page_html(source_url)

    @router.get(LOGIN_PAGE, include_in_schema=False)
    def login_page() -> HTMLResponse:
        return HTMLResponse(
            page,
            headers={"Content-Security-Policy": LOGIN_CSP, "Cache-Control": "no-store"},
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
                presented_token=request.cookies.get(names.session),
            )
        except ThrottledError as error:
            raise _throttled(error) from None
        except InvalidCredentialsError:
            raise ProblemError("invalid_credentials") from None
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
