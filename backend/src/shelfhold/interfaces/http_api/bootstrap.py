"""The first account, made in the first-run wizard (M3 ENF-UX-001, EF-UX-001).

Two anonymous routes, both closed by the facts of the instance and not by a session:

- `GET /api/v1/setup/status`: does a new instance still wait for its first account, and did the
  installer put a secret in the compose. It tells nothing else (no name, no count).
- `POST /api/v1/setup/admin`: makes the first administrator and signs in. It needs the secret of
  the compose (`SHELFHOLD_SETUP_TOKEN`), it counts a wrong one like a wrong password, and it makes
  nothing once an administrator exists, from the wizard or from the host command.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, SecretStr

from shelfhold.identity.public import (
    MAX_PASSWORD_LENGTH,
    AdminExistsError,
    SetupTokenInvalidError,
    SetupTokenNotConfiguredError,
    ThrottledError,
)
from shelfhold.interfaces.http_api.auth import SETUP_ADMIN_PATH, gate_of, set_session_cookies
from shelfhold.interfaces.http_api.login import CsrfToken
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.ratelimit import client_ip_of

__all__ = ["build_bootstrap_router"]


class SetupStatusOut(BaseModel):
    needs_admin: bool
    token_configured: bool


class FirstAdminIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    setup_token: str = Field(max_length=256)  # the secret of the compose, never stored or answered


def build_bootstrap_router(setup_token: SecretStr | None) -> APIRouter:
    router = APIRouter()
    expected = setup_token.get_secret_value() if setup_token is not None else None

    @router.get("/api/v1/setup/status", response_model=SetupStatusOut)
    def first_run_status(request: Request) -> SetupStatusOut:
        return SetupStatusOut(
            needs_admin=gate_of(request).auth.needs_admin(), token_configured=bool(expected)
        )

    @router.post(SETUP_ADMIN_PATH, response_model=CsrfToken, status_code=201)
    def make_first_admin(body: FirstAdminIn, request: Request) -> Response:
        gate = gate_of(request)
        names = gate.cookies_for(request.scope)
        try:
            issued = gate.auth.bootstrap_admin(
                body.username,
                body.password,
                presented_token=body.setup_token,
                expected_token=expected,
                ip=client_ip_of(request.scope),
                user_agent=request.headers.get("user-agent"),
            )
        except AdminExistsError:
            raise ProblemError("setup_closed") from None
        except SetupTokenNotConfiguredError:
            raise ProblemError("setup_token_not_configured") from None
        except SetupTokenInvalidError:
            raise ProblemError("setup_token_invalid") from None
        except ThrottledError as error:
            raise ProblemError(
                "too_many_attempts",
                extensions={"retry_after_s": error.retry_after_s},
                headers={"Retry-After": str(error.retry_after_s)},
            ) from None
        except ValueError as error:  # a name or a password that is not one: nothing was made
            raise ProblemError("profile_refused", extensions={"reason": str(error)}) from None
        response = JSONResponse({"csrf_token": issued.csrf_secret}, status_code=201)
        set_session_cookies(response, names, issued)
        return response

    return router
