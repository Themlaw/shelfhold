"""Who is calling: session cookie or API key, CSRF, identity headers (EF-USR-021, EF-USR-024).

`AuthMiddleware` sits inside the request context and outside the idempotency ledger, so that a
key is always scoped to the real actor and a refused request never reaches the ledger. It:

- never sees a proxy identity header: `IdentityHeaderFilter` (outermost) removed them (P5);
- resolves the principal: `Authorization: Bearer shk_…` first, else the session cookie;
- on a state-changing request carried by a session, requires `X-CSRF-Token` equal to the
  session's CSRF secret (synchronizer token delivered by a readable cookie, constant-time
  comparison) and refuses `Sec-Fetch-Site: cross-site`; API keys are not ambient credentials
  and need no CSRF token;
- sends the session cookie again when its expiry slid.

Cookies are `HttpOnly`, `SameSite=Strict` and, over HTTPS, `Secure` with the `__Host-` prefix.
Over plain `http://` (LAN address, no TLS) a `Secure` cookie would never come back, so the
cookie is set without it and without the prefix; HTTPS is decided by the request scheme or an
`https://` public URL (docs/admin/durcir.md).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.cookies import SimpleCookie
from uuid import uuid4

import sqlalchemy as sa
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shelfhold.identity.public import (
    DEFAULT_RESERVED_ROLES,
    IDENTITY_HEADER_AUDIT,
    Credential,
    CredentialNotAllowed,
    IssuedSession,
    LocalAuth,
    Principal,
    ReauthRequired,
    ReservedAction,
    RoleNotAllowed,
    SessionAuth,
    same_secret,
)
from shelfhold.infrastructure.http_headers import BEARER_CHALLENGE
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.idempotency import NEVER_STORE
from shelfhold.interfaces.http_api.problems import ProblemError, build_problem_response

LOGIN_PAGE = "/login"
LOGIN_PATH = "/api/v1/auth/login"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class CookieNames:
    session: str
    csrf: str
    device: str  # signed proof of a past login on this browser (attempt limiting only)
    secure: bool


SECURE_COOKIES = CookieNames("__Host-sid", "__Host-csrf", "__Host-dev", secure=True)
PLAIN_COOKIES = CookieNames("sid", "csrf", "dev", secure=False)
DEVICE_COOKIE_MAX_AGE_S = 365 * 24 * 3600


@dataclass
class AuthGate:
    """Everything the HTTP side needs from identity, plus the cookie decisions."""

    auth: LocalAuth
    public_url: str | None = None

    def cookies_for(self, scope: Scope) -> CookieNames:
        https = scope.get("scheme") == "https" or (self.public_url or "").startswith("https://")
        return SECURE_COOKIES if https else PLAIN_COOKIES

    def record_stripped_header(self, header: str) -> None:
        """Audit entry read back by `shelfhold status` (one per header name and process)."""
        self.auth.audit_refusal(IDENTITY_HEADER_AUDIT, target=header)


def _cookie_header(
    name: str,
    value: str,
    *,
    names: CookieNames,
    http_only: bool,
    max_age: int | None,
) -> str:
    cookie: SimpleCookie = SimpleCookie()
    cookie[name] = value
    morsel = cookie[name]
    morsel["path"] = "/"
    morsel["samesite"] = "Strict"
    if http_only:
        morsel["httponly"] = True
    if names.secure:
        morsel["secure"] = True
    if max_age is not None:
        morsel["max-age"] = max_age
    return morsel.OutputString()


def set_session_cookies(response: Response, names: CookieNames, issued: IssuedSession) -> None:
    for name, value, http_only in (
        (names.session, issued.token, True),
        (names.csrf, issued.csrf_secret, False),  # read by the page, sent back as a header
    ):
        response.headers.append(
            "set-cookie",
            _cookie_header(
                name, value, names=names, http_only=http_only, max_age=issued.cookie_max_age_s
            ),
        )
    if issued.device_proof is not None:
        response.headers.append(
            "set-cookie",
            _cookie_header(
                names.device,
                issued.device_proof,
                names=names,
                http_only=True,
                max_age=DEVICE_COOKIE_MAX_AGE_S,
            ),
        )
    response.headers["Cache-Control"] = "no-store"


def clear_session_cookies(response: Response, names: CookieNames) -> None:
    for name, http_only in ((names.session, True), (names.csrf, False)):
        response.headers.append(
            "set-cookie", _cookie_header(name, "", names=names, http_only=http_only, max_age=0)
        )


def _cookie_value(headers: Headers, name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel is not None and morsel.value else None


def _bearer(headers: Headers) -> str | None:
    parts = headers.get("authorization", "").split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


class AuthMiddleware:
    def __init__(self, app: ASGIApp, gate: AuthGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["principal"] = None
        headers = Headers(scope=scope)
        names = self.gate.cookies_for(scope)

        principal, session = await self._resolve(headers, names)
        state["principal"] = principal

        unsafe = scope["method"] not in SAFE_METHODS
        by_key = principal is not None and session is None
        if unsafe and not by_key and headers.get("sec-fetch-site", "").lower() == "cross-site":
            await self._problem(scope, receive, send, "csrf_failed")
            return
        if unsafe and scope["path"] == LOGIN_PATH:
            content_type = headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":  # a cross-site form cannot send JSON
                await self._problem(scope, receive, send, "csrf_failed")
                return
        elif unsafe and session is not None:
            presented = headers.get(CSRF_HEADER.lower(), "")
            if not presented or not same_secret(presented, session.csrf_secret):
                await self._problem(scope, receive, send, "csrf_failed")
                return

        if session is None or not session.touched:
            await self.app(scope, receive, send)
            return
        token = _cookie_value(headers, names.session) or ""
        # Both cookies slide together: a page that lost `csrf` while `sid` lives could not write.
        refreshed = [
            _cookie_header(name, value, names=names, http_only=http_only, max_age=max_age)
            for name, value, http_only, max_age in (
                (names.session, token, True, session.cookie_max_age_s),
                (names.csrf, session.csrf_secret, False, session.cookie_max_age_s),
            )
        ]

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = MutableHeaders(scope=message)
                if not any(v.startswith(f"{names.session}=") for v in out.getlist("set-cookie")):
                    for line in refreshed:
                        out.append("set-cookie", line)
            await send(message)

        await self.app(scope, receive, send_with_cookie)

    async def _resolve(
        self, headers: Headers, names: CookieNames
    ) -> tuple[Principal | None, SessionAuth | None]:
        auth = self.gate.auth
        try:
            bearer = _bearer(headers)
            if bearer is not None:  # explicit credential: the cookie is not looked at
                return await run_in_threadpool(auth.authenticate_api_key, bearer), None
            token = _cookie_value(headers, names.session)
            if token is None:
                return None, None
            session = await run_in_threadpool(auth.authenticate_session, token)
        except sa.exc.OperationalError as error:  # database not migrated yet, or locked
            structlog.get_logger().warning("auth.store_unavailable", error=str(error))
            return None, None
        return (session.principal, session) if session is not None else (None, None)

    @staticmethod
    async def _problem(scope: Scope, receive: Receive, send: Send, code: str) -> None:
        request_id = scope.get("state", {}).get("request_id")
        response = build_problem_response(error_code(code), request_id=request_id)
        await response(scope, receive, send)


def actor_of(scope: Scope) -> str:
    """Idempotency actor: the authenticated principal; an anonymous caller shares nothing."""
    principal = scope.get("state", {}).get("principal")
    if isinstance(principal, Principal):
        return principal.actor_id
    return f"anonymous:{uuid4().hex}"


# --- dependencies ------------------------------------------------------------------------------


class LoginRequiredError(Exception):
    """No valid credential: a page navigation goes to the login page, an API client gets 401."""


def gate_of(request: Request) -> AuthGate:
    gate = getattr(request.app.state, "auth_gate", None)
    if not isinstance(gate, AuthGate):
        raise RuntimeError("auth_gate_missing")  # built without `auth`: nobody can log in
    return gate


def current_principal(request: Request) -> Principal | None:
    principal = request.scope.get("state", {}).get("principal")
    return principal if isinstance(principal, Principal) else None


def require_principal(request: Request) -> Principal:
    principal = current_principal(request)
    if principal is None:
        raise LoginRequiredError
    if request.method not in SAFE_METHODS and not principal.may_write:
        raise ProblemError("forbidden")
    return principal


def require_session(request: Request) -> Principal:
    principal = require_principal(request)
    if principal.credential is not Credential.SESSION:
        raise ProblemError("credential_not_allowed")
    return principal


def reserved_action(
    action: ReservedAction, *, roles: frozenset[str] | Sequence[str] = DEFAULT_RESERVED_ROLES
) -> Callable[[Request], Principal]:
    """Dependency of a route that performs a reserved action (EF-USR-024, EF-USR-022 (2)).

    Sessions of any role but `admin` are refused (403 `forbidden`) unless `roles=` says so: a
    route that lets `member` or `child` do something reserved (e.g. rate) must name it."""
    allowed = frozenset(roles)

    def dependency(request: Request) -> Principal:
        # First, before any refusal: the answer of a reserved action is never kept for replay.
        request.scope.setdefault("state", {})[NEVER_STORE] = True
        principal = require_principal(request)
        auth = gate_of(request).auth
        verdict = auth.authorize(
            principal,
            action,
            restricted_profile_exists=auth.restricted_profile_exists(),
            allowed_roles=allowed,
        )
        if isinstance(verdict, RoleNotAllowed):
            raise ProblemError("forbidden", extensions={"action": action.name})
        if isinstance(verdict, CredentialNotAllowed):
            raise ProblemError("credential_not_allowed", extensions={"action": action.name})
        if isinstance(verdict, ReauthRequired):
            methods = list(auth.reauth_method_names)
            raise ProblemError(
                "reauth_required",
                extensions={
                    "action": action.name,
                    "auth_age_s": verdict.auth_age_s,
                    "max_age_s": verdict.max_age_s,
                    "reauth_method": methods[0] if methods else None,
                    "reauth_methods": methods,
                },
            )
        return principal

    dependency.__name__ = f"reserved_{action.name.replace('.', '_')}"
    return dependency


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return request.method in {"GET", "HEAD"} and "text/html" in accept.split(",")[0]


def install_auth(app: FastAPI, gate: AuthGate | None) -> None:
    app.state.auth_gate = gate

    @app.exception_handler(LoginRequiredError)
    def _login_required(request: Request, _exc: Exception) -> Response:
        if _wants_html(request):
            return RedirectResponse(LOGIN_PAGE, status_code=303)
        request_id = getattr(request.state, "request_id", None)
        response: JSONResponse = build_problem_response(
            error_code("unauthenticated"),
            request_id=request_id if isinstance(request_id, str) else None,
            extensions={"login_url": LOGIN_PAGE},
            headers={"WWW-Authenticate": BEARER_CHALLENGE},
        )
        return response
