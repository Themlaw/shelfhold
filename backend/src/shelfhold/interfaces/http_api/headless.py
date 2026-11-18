"""`GET|PATCH /api/v1/config`, `POST /api/v1/targets`, `POST /api/v1/sources` (EF-INT-024).

The same facade as the CLI. Creating a Kavita target is the reserved action `admin_risk.accept`
(a session of an admin, fresh; never an API key); setting a source secret is `source.set_secret`.
Every `Location` is built from the configured public URL (ENF-SEC-016), never from the request;
without one the header is simply absent.
"""

from collections.abc import Callable
from contextlib import suppress
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from shelfhold.export.domain.bindings import BindingError
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
# the service writes its own audit line, with its target, in the transaction that does the thing
_ADD_KOMGA = reserved_action(RESERVED_ACTIONS["target.add_komga"], audit=False)
_SYNC_PROGRESS = reserved_action(RESERVED_ACTIONS["destination.sync_progress"], audit=False)


class ConfigRow(BaseModel):
    key: str
    value: Any
    source: str
    default: Any = None
    overridden_by: str | None = None
    warning: str | None = None  # accepted but not acting yet (EF-SRC-025)
    level: Literal["essential", "advanced", "expert"]  # the screen's level (EF-UX-009)
    locked: bool  # the screen greys it: fixed by this release or set by the environment
    secret: bool  # no value is answered for it


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


class NativeFolderIn(BaseModel):
    name: str = Field(min_length=1)
    path_map: list[dict[str, str]] = Field(min_length=1)


class KomgaIn(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    api_key: str = Field(min_length=1, repr=False)  # the key of a dedicated administrator account
    path_map: list[dict[str, str]] | None = None


class SyncProgressIn(BaseModel):
    enabled: bool


class SyncProgressOut(BaseModel):
    destination_id: str
    sync_progress: bool


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
                ConfigRow(key=v.key, value=None if v.secret else v.value, source=v.source,
                          default=None if v.secret else v.default,
                          overridden_by=v.overridden_by, warning=v.warning,
                          level=v.level, locked=v.locked, secret=v.secret)
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
            # a field the environment sets is refused here too, whatever the screen greys, and
            # before anything is written: a batch is applied whole or not at all (EF-UX-009)
            if any(admin.get(key).locked for key in body.values):
                raise ProblemError("config.key_readonly")
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

    @router.post("/targets/native-folder", status_code=201)
    def add_native_folder(
        body: NativeFolderIn,
        response: Response,
        principal: Principal = Depends(_PATH_MAP),  # noqa: B008
    ) -> Created:
        """EF-EXP-010: a folder Mihon or the local source of Suwayomi reads. Which folders are
        written is a reserved action (admin, fresh session, never a key, a child or a member), and
        a root that meets the local source of Suwayomi is refused
        (`target.root_overlaps_local_source`)."""
        try:
            added = get_admin().target_add_native_folder(
                name=body.name, path_map=body.path_map, actor=_actor_of(principal)
            )
        except BindingError as error:
            raise ProblemError(error.code) from None
        except HeadlessError as error:
            raise _problem(error) from None
        located(response, f"/api/v1/targets/{added.id}")
        return Created(id=added.id)

    @router.post("/targets/komga", status_code=201)
    def add_komga(
        body: KomgaIn,
        response: Response,
        principal: Principal = Depends(_ADD_KOMGA),  # noqa: B008
    ) -> Created:
        """EF-EXP-020: a Komga with the API key of a dedicated administrator account. The key is
        shown to Komga once, sealed, and never answered back. Reserved (a secret): a fresh admin
        session or the host CLI, never a key, a member or a child."""
        try:
            added = get_admin().target_add_komga(
                name=body.name, url=body.url, api_key=body.api_key, path_map=body.path_map,
                actor=_actor_of(principal),
            )  # fmt: skip
        except HeadlessError as error:
            raise _problem(error) from None
        located(response, f"/api/v1/targets/{added.id}")
        return Created(id=added.id)

    @router.post("/targets/cwa", status_code=201)
    def add_cwa(
        body: NativeFolderIn,
        response: Response,
        principal: Principal = Depends(_PATH_MAP),  # noqa: B008
    ) -> Created:
        """EF-EXP-022: the drop box of a Calibre-Web Automated. Which folders are written is a
        reserved action, like a native folder."""
        try:
            added = get_admin().target_add_cwa(
                name=body.name, path_map=body.path_map, actor=_actor_of(principal)
            )
        except BindingError as error:
            raise ProblemError(error.code) from None
        except HeadlessError as error:
            raise _problem(error) from None
        located(response, f"/api/v1/targets/{added.id}")
        return Created(id=added.id)

    @router.post("/destinations/{destination_id}/sync-progress")
    def sync_progress(
        destination_id: str,
        body: SyncProgressIn,
        principal: Principal = Depends(_SYNC_PROGRESS),  # noqa: B008
    ) -> SyncProgressOut:
        """EF-LEC-014: turn the progress sync of a binding on or off. Reserved (a fresh admin
        session or the host CLI, never a key, a member or a child) and journaled; a destination
        that keeps no reading position answers 409 `destination.sync_unsupported`."""
        try:
            done = get_admin().destination_sync_progress(
                destination_id=destination_id, enabled=body.enabled, actor=_actor_of(principal)
            )
        except HeadlessError as error:
            raise _problem(error) from None
        return SyncProgressOut(destination_id=done.destination_id, sync_progress=done.sync_progress)

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
