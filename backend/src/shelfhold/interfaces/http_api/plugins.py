"""Per-source settings and attested trust values (EF-SRC-013, EF-EXP-065 (5), D15).

- `GET /api/v1/sources/{key}/settings`: the plain values, and of each secret and of the cookie
  jar only whether it is set and a short fingerprint. Nothing reads a secret back.
- `PATCH .../settings`: plain values only (an admin session or key with write rights).
- `PUT|DELETE .../secrets/{field}`, `PUT .../cookies`: the reserved action `source.set_secret`
  (a fresh session, never a key), audited.
- `GET|POST /api/v1/plugins/{key}/attestations`, `DELETE .../attestations/{field}`: the reserved
  action `plugin.attest` (and `plugin.attest_partitioning` for `access_partitioning`), audited.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from shelfhold.headless.actor import Actor
from shelfhold.identity.public import RESERVED_ACTIONS, Credential, Principal, audit_actor
from shelfhold.interfaces.http_api.auth import require_principal, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.plugin_host.attestation import (
    AttestationError,
    AttestationNotNeeded,
    AttestationStore,
)
from shelfhold.plugin_host.settings import (
    SecretState,
    SettingsSchema,
    SettingsView,
    SettingValueError,
    SourceSettings,
)

__all__ = ["build_plugins_router"]

_SECRET = reserved_action(RESERVED_ACTIONS["source.set_secret"])
_ATTEST = reserved_action(RESERVED_ACTIONS["plugin.attest"])
_ATTEST_PARTITIONING = reserved_action(RESERVED_ACTIONS["plugin.attest_partitioning"])


class SecretStateOut(BaseModel):
    set: bool
    fingerprint: str | None = None


class SettingsOut(BaseModel):
    values: dict[str, Any]
    secrets: dict[str, SecretStateOut]
    cookies: SecretStateOut


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(min_length=1)


class SecretIn(BaseModel):
    value: str = Field(min_length=1, repr=False)


class CookiesIn(BaseModel):
    cookies: dict[str, str] = Field(repr=False)


class AttestationIn(BaseModel):
    field: str
    value: str


class AttestationOut(BaseModel):
    plugin_key: str
    field: str
    value: str
    plugin_version: str
    attested_at: datetime


class AttestationsOut(BaseModel):
    items: list[AttestationOut]


def _state(state: SecretState) -> SecretStateOut:
    return SecretStateOut(set=state.set, fingerprint=state.fingerprint)


def _settings(view: SettingsView) -> SettingsOut:
    return SettingsOut(
        values=view.values,
        secrets={name: _state(s) for name, s in view.secrets.items()},
        cookies=_state(view.cookies),
    )


def _actor(principal: Principal) -> Actor:
    return Actor(audit_actor(principal), principal.session_id, principal.auth_time)


def _invalid(error: SettingValueError) -> ProblemError:
    # the field and a closed reason; the value never leaves
    return ProblemError(
        "source.setting_invalid", extensions={"field": error.field, "reason": error.reason}
    )


def build_plugins_router(
    settings: Callable[[], SourceSettings],
    attestations: Callable[[], AttestationStore],
    schema_for: Callable[[str], SettingsSchema | None],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["sources"])

    def admin_only(principal: Principal = Depends(require_principal)) -> Principal:  # noqa: B008
        if principal.role != "admin":
            raise ProblemError("forbidden")
        return principal

    def schema(key: str) -> SettingsSchema:
        found = schema_for(key)
        if found is None:
            raise ProblemError("plugin.unknown")
        return found

    @router.get("/sources/{key}/settings")
    def read_settings(key: str, _principal: Principal = Depends(admin_only)) -> SettingsOut:  # noqa: B008
        return _settings(settings().view(key, schema(key)))

    @router.patch("/sources/{key}/settings")
    def patch_settings(
        key: str,
        body: SettingsPatch,
        principal: Principal = Depends(admin_only),  # noqa: B008
    ) -> SettingsOut:
        try:
            view = settings().set_values(key, schema(key), body.values, actor=_actor(principal))
        except SettingValueError as error:
            raise _invalid(error) from None
        return _settings(view)

    @router.put("/sources/{key}/secrets/{name}")
    def put_secret(
        key: str,
        name: str,
        body: SecretIn,
        principal: Principal = Depends(_SECRET),  # noqa: B008
    ) -> SecretStateOut:
        try:
            state = settings().set_secret(
                key, schema(key), name, body.value, actor=_actor(principal)
            )
        except SettingValueError as error:
            raise _invalid(error) from None
        return _state(state)

    @router.delete("/sources/{key}/secrets/{name}")
    def delete_secret(
        key: str,
        name: str,
        principal: Principal = Depends(_SECRET),  # noqa: B008
    ) -> SecretStateOut:
        try:
            state = settings().clear_secret(key, schema(key), name, actor=_actor(principal))
        except SettingValueError as error:
            raise _invalid(error) from None
        return _state(state)

    @router.put("/sources/{key}/cookies")
    def put_cookies(
        key: str,
        body: CookiesIn,
        principal: Principal = Depends(_SECRET),  # noqa: B008
    ) -> SecretStateOut:
        schema(key)  # a known source only
        try:
            state = settings().set_cookies(key, body.cookies, actor=_actor(principal))
        except SettingValueError as error:
            raise _invalid(error) from None
        return _state(state)

    def listed(key: str) -> AttestationsOut:
        return AttestationsOut(
            items=[
                AttestationOut(
                    plugin_key=a.plugin_key,
                    field=a.field,
                    value=a.value,
                    plugin_version=a.plugin_version,
                    attested_at=a.attested_at,
                )
                for a in attestations().listed(key)
            ]
        )

    @router.get("/plugins/{key}/attestations")
    def read_attestations(key: str, _principal: Principal = Depends(admin_only)) -> AttestationsOut:  # noqa: B008
        return listed(key)

    @router.post("/plugins/{key}/attestations")
    def attest(
        key: str,
        body: AttestationIn,
        request: Request,
        principal: Principal = Depends(_ATTEST),  # noqa: B008
    ) -> AttestationOut:
        if body.field == "access_partitioning":
            _ATTEST_PARTITIONING(request)  # which shelves it may serve: parental control too
        user = principal.user_id if principal.credential is Credential.SESSION else None
        try:
            given = attestations().attest(
                key, body.field, body.value, actor=_actor(principal), user_id=user
            )
        except AttestationNotNeeded:
            raise ProblemError("plugin.attestation_not_needed") from None
        except AttestationError:
            raise ProblemError("plugin.attestation_invalid") from None
        return AttestationOut(
            plugin_key=given.plugin_key, field=given.field, value=given.value,
            plugin_version=given.plugin_version, attested_at=given.attested_at,
        )  # fmt: skip

    @router.delete("/plugins/{key}/attestations/{name}")
    def withdraw(
        key: str,
        name: str,
        principal: Principal = Depends(_ATTEST),  # noqa: B008
    ) -> AttestationsOut:
        attestations().withdraw(key, name, actor=_actor(principal))
        return listed(key)

    return router
