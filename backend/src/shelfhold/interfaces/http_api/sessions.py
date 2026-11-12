"""Sessions and the recovery kit (M3 L07, EF-USR-011, EF-USR-023).

- `/auth/sessions`: the caller's OWN live sessions, closed one by one or all but the current one.
  Only a session credential: a key or a device token is refused. The owner is the principal's,
  never a request's, so a session of another profile answers like an id that does not exist.
- `/admin/sessions`: every profile's sessions and the closing of any, a reserved action (admin
  session, fresh in the hardened profile, never a key, a token or a member).
- `/admin/recovery-kit`: the printable page, no secret in it. There is NO route that creates,
  accepts or lists a recovery code: the way back in is `admin reset-password` on the host.
"""

import base64
import hashlib
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from shelfhold.identity.public import (
    RECOVERY_KIT_STYLE,
    RESERVED_ACTIONS,
    Principal,
    SessionView,
    UnknownSessionError,
    render_recovery_kit,
)
from shelfhold.interfaces.http_api.auth import gate_of, require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

SESSIONS_PATH = "/api/v1/auth/sessions"
ADMIN_SESSIONS_PATH = "/api/v1/admin/sessions"
RECOVERY_KIT_PATH = "/api/v1/admin/recovery-kit"

_LIST_ALL = reserved_action(RESERVED_ACTIONS["session.list_all"])
_REVOKE_OTHER = reserved_action(RESERVED_ACTIONS["session.revoke_other"])
_KIT = reserved_action(RESERVED_ACTIONS["recovery_kit.read"])

_STYLE_HASH = base64.b64encode(hashlib.sha256(RECOVERY_KIT_STYLE.encode()).digest()).decode()
KIT_CSP = (
    f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class SessionItem(BaseModel):
    id: str
    device: str | None
    ip: str | None
    created_at: datetime
    auth_time: datetime
    last_seen_at: datetime
    remember: bool
    current: bool


class AdminSessionItem(SessionItem):
    user_id: str
    username: str


class SessionList(BaseModel):
    items: list[SessionItem]


class AdminSessionList(BaseModel):
    items: list[AdminSessionItem]


class Revoked(BaseModel):
    revoked: int


def _item(view: SessionView, current_id: str | None) -> dict[str, object]:
    return {
        "id": view.id,
        "device": view.device,
        "ip": view.ip,
        "created_at": view.created_at,
        "auth_time": view.auth_time,
        "last_seen_at": view.last_seen_at,
        "remember": view.remember,
        "current": view.id == current_id,
    }


def build_sessions_router() -> APIRouter:
    router = APIRouter()

    @router.get(SESSIONS_PATH, response_model=SessionList)
    def sessions_list(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> SessionList:
        views = gate_of(request).auth.sessions_list(principal)
        return SessionList(items=[SessionItem(**_item(v, principal.session_id)) for v in views])

    @router.post(f"{SESSIONS_PATH}/revoke-others", response_model=Revoked)
    def sessions_revoke_others(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> Revoked:
        return Revoked(revoked=gate_of(request).auth.sessions_revoke_others(principal))

    @router.delete(f"{SESSIONS_PATH}/{{session_id}}", status_code=204)
    def session_revoke(
        session_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Response:
        try:
            gate_of(request).auth.session_revoke(principal, session_id)
        except UnknownSessionError:
            raise ProblemError("session_not_found") from None
        return Response(status_code=204)

    @router.get(ADMIN_SESSIONS_PATH, response_model=AdminSessionList)
    def admin_sessions_list(
        request: Request, principal: Annotated[Principal, Depends(_LIST_ALL)]
    ) -> AdminSessionList:
        views = gate_of(request).auth.sessions_list_all(principal)
        return AdminSessionList(
            items=[
                AdminSessionItem(
                    **_item(v, principal.session_id), user_id=v.user_id, username=v.username
                )
                for v in views
            ]
        )

    @router.delete(f"{ADMIN_SESSIONS_PATH}/{{session_id}}", status_code=204)
    def admin_session_revoke(
        session_id: str,
        request: Request,
        principal: Annotated[Principal, Depends(_REVOKE_OTHER)],
    ) -> Response:
        try:
            gate_of(request).auth.session_revoke_of_other(principal, session_id)
        except UnknownSessionError:
            raise ProblemError("session_not_found") from None
        return Response(status_code=204)

    @router.get(RECOVERY_KIT_PATH, response_class=HTMLResponse)
    def recovery_kit(_principal: Annotated[Principal, Depends(_KIT)]) -> HTMLResponse:
        return HTMLResponse(
            render_recovery_kit(),
            headers={"Content-Security-Policy": KIT_CSP, "Cache-Control": "no-store"},
        )

    return router
