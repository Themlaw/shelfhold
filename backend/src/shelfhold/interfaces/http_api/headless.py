"""`GET|PATCH /api/v1/config`, `POST /api/v1/targets`, `POST /api/v1/sources` (EF-INT-024).

The same facade as the CLI. Creating a Kavita target is the reserved action `admin_risk.accept`
(a session of an admin, fresh; never an API key); setting a source secret is `source.set_secret`.
Every `Location` is built from the configured public URL (ENF-SEC-016), never from the request;
without one the header is simply absent.
"""

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from shelfhold.headless import (
    AdminRiskNotAccepted,
    HeadlessAdmin,
    HeadlessError,
    PublicUrlMissing,
    absolute_url,
)
from shelfhold.headless.actor import Actor
from shelfhold.headless.keys import PATH_MAP_PREFIX, SECRET_KEYS
from shelfhold.identity.public import RESERVED_ACTIONS, Credential, Principal, audit_actor
from shelfhold.interfaces.http_api.auth import require_principal, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_headless_router"]

_RISK = RESERVED_ACTIONS["admin_risk.accept"]
_SECRET = RESERVED_ACTIONS["source.set_secret"]
_PATH_MAP = reserved_action(RESERVED_ACTIONS["config.path_map"])


class ConfigRow(BaseModel):
    key: str
    value: Any
    source: str
    default: Any = None
    overridden_by: str | None = None
    warning: str | None = None  # accepted but not acting yet (EF-SRC-025)


class ConfigOut(BaseModel):
    items: list[ConfigRow]


class ConfigPatch(BaseModel):
    values: dict[str, str] = Field(min_length=1)


class TargetIn(BaseModel):
    kind: str = "kavita"
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = Field(min_length=1, repr=False)
    accept_admin_risk: bool = False
    path_map: list[dict[str, str]] | None = None


class SourceIn(BaseModel):
    kind: str = "suwayomi"
    url: str = Field(min_length=1)
    secret: str | None = Field(default=None, repr=False)  # user:password
    fix_suwayomi_settings: bool = False  # the admin confirms: switch off the two update filters


class Created(BaseModel):
    id: str


def _problem(error: HeadlessError) -> ProblemError:
    return ProblemError(error.code)


def build_headless_router(
    get_admin: Callable[[], HeadlessAdmin], get_public_url: Callable[[], str | None]
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["configuration"])

    def located(response: Response, path: str) -> None:
        # no public URL configured: no absolute link (never one built from the request)
        with suppress(PublicUrlMissing):
            response.headers["Location"] = absolute_url(get_public_url(), path)

    def rows(values: list[Any]) -> ConfigOut:
        return ConfigOut(
            items=[
                ConfigRow(key=v.key, value=v.value, source=v.source, default=v.default,
                          overridden_by=v.overridden_by, warning=v.warning)
                for v in values
            ]
        )  # fmt: skip

    def admin_only(principal: Principal = Depends(require_principal)) -> Principal:  # noqa: B008
        if principal.role != "admin":
            raise ProblemError("forbidden")
        return principal

    @router.get("/config")
    def read_config(_principal: Principal = Depends(admin_only)) -> ConfigOut:  # noqa: B008
        return rows(get_admin().list())

    @router.patch("/config")
    def patch_config(
        body: ConfigPatch,
        request: Request,
        principal: Principal = Depends(admin_only),  # noqa: B008
    ) -> ConfigOut:
        if SECRET_KEYS & body.values.keys():  # a channel URL is a secret: the CLI or the file
            raise ProblemError("credential_not_allowed")
        if "public_url" in body.values and principal.credential is not Credential.SESSION:
            raise ProblemError("credential_not_allowed")  # where links point: a session, no key
        if any(k.startswith(PATH_MAP_PREFIX) for k in body.values):
            _PATH_MAP(request)  # which folders are written: a reserved action, fresh session (P3)
        admin = get_admin()
        try:
            for key, value in body.values.items():
                admin.set(key, value)
        except HeadlessError as error:
            raise _problem(error) from None
        return rows(admin.list())

    @router.post("/targets", status_code=201)
    def add_target(
        body: TargetIn,
        response: Response,
        principal: Principal = Depends(reserved_action(_RISK)),  # noqa: B008
    ) -> Created:
        if body.kind != "kavita":
            raise ProblemError("config.invalid_value")
        try:
            added = get_admin().target_add_kavita(
                name=body.name, url=body.url, username=body.username, password=body.password,
                accept_admin_risk=body.accept_admin_risk, path_map=body.path_map,
                actor=_actor_of(principal),
            )  # fmt: skip
        except AdminRiskNotAccepted:
            raise ProblemError("target.admin_risk_not_accepted") from None
        except HeadlessError as error:
            raise _problem(error) from None
        located(response, f"/api/v1/targets/{added.id}")
        return Created(id=added.id)

    @router.post("/sources", status_code=201)
    def add_source(
        body: SourceIn,
        response: Response,
        principal: Principal = Depends(reserved_action(_SECRET)),  # noqa: B008
    ) -> Created:
        if body.kind != "suwayomi":
            raise ProblemError("config.invalid_value")
        try:
            added = get_admin().source_add_suwayomi(
                url=body.url,
                secret=body.secret,
                fix_suwayomi_settings=body.fix_suwayomi_settings,
                actor=_actor_of(principal),
            )
        except HeadlessError as error:
            raise _problem(error) from None
        located(response, f"/api/v1/sources/{added.plugin_id}")
        return Created(id=added.plugin_id)

    return router


def _actor_of(principal: Principal) -> Actor:
    return Actor(audit_actor(principal), principal.session_id, principal.auth_time)
