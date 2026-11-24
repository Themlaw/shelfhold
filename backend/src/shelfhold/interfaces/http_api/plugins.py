"""Per-source settings and attested trust values (EF-SRC-013, EF-EXP-065 (5), D15).

- `GET /api/v1/sources/{key}/settings`: the plain values, and of each secret and of the cookie
  jar only whether it is set and a short fingerprint. Nothing reads a secret back.
- `PATCH .../settings`: plain values only (an admin session or key with write rights).
- `PUT|DELETE .../secrets/{field}`, `PUT .../cookies`: the reserved action `source.set_secret`
  (a fresh session, never a key), audited.
- `GET|POST /api/v1/plugins/{key}/attestations`, `DELETE .../attestations/{field}`: the reserved
  action `plugin.attest` (and `plugin.attest_partitioning` for `access_partitioning`), audited.
- `GET|POST /api/v1/plugin-repos`, `DELETE .../plugin-repos/{url}`, `POST .../refresh`: the
  repositories of signed plugin indexes (M6 L07, D14, EF-SRC-017). Adding and removing are the
  reserved actions `plugin.index_add` and `plugin.index_remove` (a fresh admin session or the
  host CLI, never a key); the add pins the URL **and** the public key of the signer — without
  the key there is no pin. A refresh goes through the host's Fetcher and verifies the minisign
  signature, the trusted comment, the monotone version, the expiry and the revocations.
- `GET /api/v1/plugins`: every installed third-party plugin, its mode and why it is not loaded
  (EF-SRC-019: the UI explains).
- `POST|DELETE /api/v1/plugins/{name}/enabled`: the reserved action `plugin.enable_isolated`
  (M6 L06, D13) — a fresh admin session or the host CLI, never a key. The in-process mode is
  never enabled over the wire: its typed phrase cannot cross it.
- `POST|DELETE /api/v1/plugins/{key}/pin`, `POST .../update`, `POST .../rollback`: the lifecycle
  of an installed plugin (M6 L09a, EF-SRC-020, D15) — by its key, like the index and the
  attestations. The pin freezes the version; the update installs what the pinned index vouches
  for now, keeping the previous wheel; the rollback returns to that previous version and is the
  reserved action `plugin.rollback` (a fresh admin session or the host CLI, never a key).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from shelfhold.headless.actor import Actor
from shelfhold.i18n import negotiate, t_lang
from shelfhold.identity.public import RESERVED_ACTIONS, Credential, Principal, audit_actor
from shelfhold.interfaces.http_api.auth import require_principal, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host import lifecycle
from shelfhold.plugin_host.attestation import (
    AttestationError,
    AttestationNotNeeded,
    AttestationStore,
)
from shelfhold.plugin_host.index import PluginIndexError, RepoStore
from shelfhold.plugin_host.settings import (
    SecretState,
    SettingsSchema,
    SettingsView,
    SettingValueError,
    SourceSettings,
    write_audit,
)

__all__ = ["build_plugins_router"]

_SECRET = reserved_action(RESERVED_ACTIONS["source.set_secret"])
_ATTEST = reserved_action(RESERVED_ACTIONS["plugin.attest"])
_ATTEST_PARTITIONING = reserved_action(RESERVED_ACTIONS["plugin.attest_partitioning"])
# the audit line of the index actions carries its target (`url:key_id`), like the CLI: the
# route writes it, in the transaction that pins or unpins (audit=False) — and writes the
# refusal's own line (`plugin.index_*_refused`, `url:code`) when the store refuses
_INDEX_ADD = reserved_action(RESERVED_ACTIONS["plugin.index_add"], audit=False)
_INDEX_REMOVE = reserved_action(RESERVED_ACTIONS["plugin.index_remove"], audit=False)
# M6 L06: the gate checks the credentials and writes no line — the route writes its own, with
# the plugin as its target, once the guard has agreed (a refusal leaves no "allowed" row)
_ENABLE_ISOLATED = reserved_action(RESERVED_ACTIONS["plugin.enable_isolated"], audit=False)
_DISABLE_ISOLATED = reserved_action(RESERVED_ACTIONS["plugin.disable_isolated"], audit=False)
# M6 L09a (EF-SRC-020, D15): the rollback puts code the index no longer vouches for back —
# the gate checks the credentials and writes no line, the route writes its own once the
# previous kept version is back (a refusal leaves no "allowed" row)
_ROLLBACK = reserved_action(RESERVED_ACTIONS["plugin.rollback"], audit=False)


class SecretStateOut(BaseModel):
    set: bool
    fingerprint: str | None = None


class SettingsOut(BaseModel):
    values: dict[str, Any]
    secrets: dict[str, SecretStateOut]
    cookies: SecretStateOut
    warnings: list[str] = Field(default_factory=list)  # closed codes, e.g. `source.use_readback`


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


class RepoIn(BaseModel):
    url: str
    # optional in the schema on purpose: a repository without the signer's key is a typed
    # refusal (`plugin.index_fingerprint_required`), not a validation error
    pubkey: str | None = None


class RepoOut(BaseModel):
    url: str
    key_id: str
    last_version: int
    expires: datetime | None
    added_at: datetime


class ReposOut(BaseModel):
    items: list[RepoOut]


class RefreshOut(BaseModel):
    url: str
    version: int
    applied: bool
    rotated: bool


_INDEX_PROBLEMS = {
    "fingerprint_required": "plugin.index_fingerprint_required",
    "rollback": "plugin.index_rollback",
    "same_version": "plugin.index_rollback",
    "expired": "plugin.index_expired",
    "expires_too_far": "plugin.index_expired",
    "not_found": "plugin.index_not_found",
}


def _index_problem(error: PluginIndexError) -> ProblemError:
    code = _INDEX_PROBLEMS.get(error.code, "plugin.index_invalid")
    return ProblemError(code, detail=error.detail)


class PluginOut(BaseModel):
    """One installed third-party plugin: its mode (D13) and why it is not loaded."""

    name: str
    version: str
    mode: str
    status: str  # `enabled` in its own mode, else `disabled`
    reason: str | None = None
    notice: str | None = None  # e.g. a disable that takes effect at the next restart


class PluginsOut(BaseModel):
    items: list[PluginOut]


def _state(state: SecretState) -> SecretStateOut:
    return SecretStateOut(set=state.set, fingerprint=state.fingerprint)


def _settings(view: SettingsView) -> SettingsOut:
    return SettingsOut(
        values=view.values,
        secrets={name: _state(s) for name, s in view.secrets.items()},
        cookies=_state(view.cookies),
        warnings=list(view.warnings),
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
    data_dir: Callable[[], Path],
    engine: Callable[[], Engine],
    repos: Callable[[], RepoStore],
    index_fetcher_for: Callable[[], Any],
    installed_for: Callable[[], Mapping[str, Any]],
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

    # -- the repositories of signed plugin indexes (M6 L07, EF-SRC-017, D14) ---------------------

    def _audit(principal: Principal, action: str, target: str) -> None:
        guard.audit(
            engine(),
            action,
            target,
            actor_id=audit_actor(principal),
            session_id=principal.session_id,
            auth_time=principal.auth_time,
        )

    def _repo(record: Any) -> RepoOut:
        return RepoOut(
            url=record.url,
            key_id=record.key_id,
            last_version=record.last_version,
            expires=record.expires,
            added_at=record.added_at,
        )

    def _repos() -> ReposOut:
        return ReposOut(items=[_repo(record) for record in repos().list()])

    @router.get("/plugin-repos")
    def read_plugin_repos(_principal: Principal = Depends(admin_only)) -> ReposOut:  # noqa: B008
        return _repos()

    @router.post("/plugin-repos", status_code=201)
    def add_plugin_repo(
        body: RepoIn,
        principal: Principal = Depends(_INDEX_ADD),  # noqa: B008
    ) -> RepoOut:
        try:
            record = repos().add(body.url, body.pubkey or "", principal=principal)
        except PluginIndexError as error:
            # a refusal leaves its trace too, like the CLI: the address and the closed code
            _audit(principal, "plugin.index_add_refused", f"{body.url}:{error.code}")
            raise _index_problem(error) from None
        return _repo(record)

    @router.delete("/plugin-repos/{url:path}")
    def remove_plugin_repo(
        url: str,
        principal: Principal = Depends(_INDEX_REMOVE),  # noqa: B008
    ) -> ReposOut:
        try:
            repos().remove(url, principal=principal)
        except PluginIndexError as error:
            raise _index_problem(error) from None
        return _repos()

    @router.post("/plugin-repos/{url:path}/refresh")
    async def refresh_plugin_repo(
        url: str,
        request: Request,
        principal: Principal = Depends(admin_only),  # noqa: B008
    ) -> RefreshOut:
        record = next((r for r in repos().list() if r.url == url), None)
        if record is None:
            raise ProblemError(
                "plugin.index_not_found",
                detail=t_lang(
                    "plugin.index_not_found_detail",
                    language=negotiate(request.headers.get("accept-language")),
                ),
            )
        fetcher = index_fetcher_for()
        try:
            outcome = await repos().refresh(record, fetcher, principal=principal)
        except PluginIndexError as error:
            # a refusal leaves its trace too, like the CLI: the address and the closed code
            _audit(principal, "plugin.index_refresh_refused", f"{url}:{error.code}")
            raise _index_problem(error) from None
        finally:
            await fetcher.aclose()
        return RefreshOut(
            url=url,
            version=outcome.document.version,
            applied=outcome.applied,
            rotated=outcome.rotated,
        )

    # -- the guard of M6 L06 (D13, EF-SRC-019): the mode, the activation, the reason -----------
    # The gate checks the credentials and writes no line (`audit=False`): each route writes its
    # own, with the plugin as its target, once the guard has agreed — a refusal (unknown plugin,
    # in-process mode over the wire) leaves no row that says something was allowed.

    def _plugins() -> PluginsOut:
        layout = guard.Layout.of(data_dir())
        state = guard.read_state(layout)
        items = []
        revoked = lifecycle.revoked_dists(engine(), layout)  # an index's word, read every time
        for verdict in guard.verdicts(layout, os.environ, revoked=revoked):  # the server's env
            plugin = state.installed[verdict.name]
            items.append(
                PluginOut(
                    name=verdict.name,
                    version=plugin.version,
                    mode=verdict.mode,
                    status="enabled" if verdict.granted else "disabled",
                    reason=verdict.reason,
                )
            )
        return PluginsOut(items=items)

    @router.get("/plugins")
    def read_plugins(_principal: Principal = Depends(admin_only)) -> PluginsOut:  # noqa: B008
        return _plugins()

    def _one(layout: guard.Layout, name: str, *, notice: str | None = None) -> PluginOut:
        state = guard.read_state(layout)
        verdict = next(
            (
                v
                for v in guard.verdicts(
                    layout, os.environ, revoked=lifecycle.revoked_dists(engine(), layout)
                )
                if v.name == guard.normalise_name(name)
            ),
            None,
        )
        plugin = state.installed.get(guard.normalise_name(name))
        if verdict is None or plugin is None:
            raise ProblemError("plugin.unknown")
        return PluginOut(
            name=verdict.name,
            version=plugin.version,
            mode=verdict.mode,
            status="enabled" if verdict.granted else "disabled",
            reason=verdict.reason,
            notice=notice,
        )

    @router.post("/plugins/{name}/enabled")
    def enable_plugin(
        name: str,
        request: Request,
        principal: Principal = Depends(_ENABLE_ISOLATED),  # noqa: B008
    ) -> PluginOut:
        layout = guard.Layout.of(data_dir())
        try:
            guard.enable_isolated(layout, name)
        except guard.PluginInstallError as exc:
            if exc.code == "unknown":
                raise ProblemError("plugin.unknown") from None
            if exc.code == "mode_invalid":
                # the in-process mode keeps its typed phrase, and no phrase crosses the wire
                raise ProblemError("plugin.not_isolated") from None
            raise ProblemError("plugin.enable_refused", detail=exc.detail) from None
        state = guard.read_state(layout)
        plugin = state.installed[guard.normalise_name(name)]
        _audit(principal, "plugin.enable", f"{plugin.name}@{plugin.wheel_sha256}")
        return _one(layout, name)

    @router.delete("/plugins/{name}/enabled")
    def disable_plugin(
        name: str,
        request: Request,
        principal: Principal = Depends(_DISABLE_ISOLATED),  # noqa: B008
    ) -> PluginOut:
        layout = guard.Layout.of(data_dir())
        state = guard.read_state(layout)
        plugin = state.installed.get(guard.normalise_name(name))
        if plugin is None:
            raise ProblemError("plugin.unknown") from None
        loaded = plugin.mode == guard.MODE_IN_PROCESS and plugin.name in state.confirmed
        guard.unconfirm(layout, name)  # both activations: the plugin runs in one mode at a time
        _audit(principal, "plugin.disable", plugin.name)
        notice = None
        if loaded:
            # the host process loaded it at start-up: it goes on running until the next restart
            notice = t_lang(
                "plugin.disable_restart_notice",
                language=negotiate(request.headers.get("accept-language")),
            )
        return _one(layout, name, notice=notice)

    # -- the lifecycle of an installed plugin (M6 L09a, EF-SRC-020, D15) -------------------------
    # The pin and the update are the admin's ordinary acts; the rollback is a reserved action.
    # Each route hands its audit line to the act itself, written in the same transaction as the
    # change it vouches for: no line without the act, no act without its line. A refusal leaves
    # its own trace (`plugin.*_refused`, `key:code`) — never a row that says something moved.

    def _lifecycle_audit(principal: Principal, action: str, target: str) -> Callable[[Any], None]:
        """The audit line of a lifecycle act, handed to the act itself: it is written in the
        same transaction as the change it vouches for (M6 L09a), like the CLI's own callback."""
        actor = _actor(principal)
        return lambda connection: write_audit(connection, action, target, actor)

    def _installed_dist(name: str) -> tuple[str, str]:
        """The key and the distribution of an installed plugin: the lifecycle speaks the key,
        like the index and the attestations, never the distribution name."""
        found = installed_for().get(guard.normalise_name(name))
        if found is None:
            raise ProblemError("plugin.unknown")
        return guard.normalise_name(name), found.dist

    def _pin_route(name: str, principal: Principal, *, pinned: bool) -> PluginOut:
        key, dist = _installed_dist(name)
        layout = guard.Layout.of(data_dir())
        act = lifecycle.pin if pinned else lifecycle.unpin
        action = "plugin.pin" if pinned else "plugin.unpin"
        refused = "plugin.pin_refused" if pinned else "plugin.unpin_refused"
        try:
            act(engine(), layout, key, dist, audit=_lifecycle_audit(principal, action, key))
        except lifecycle.PluginLifecycleError as exc:
            if exc.code == "unknown":
                raise ProblemError("plugin.unknown") from None
            _audit(principal, refused, f"{key}:{exc.code}")
            raise ProblemError("plugin.pin_refused", detail=exc.detail) from None
        # the guard's view speaks the distribution name, the route was addressed by the key
        return _one(layout, dist)

    @router.post("/plugins/{name}/pin")
    def pin_plugin(name: str, principal: Principal = Depends(admin_only)) -> PluginOut:  # noqa: B008
        return _pin_route(name, principal, pinned=True)

    @router.delete("/plugins/{name}/pin")
    def unpin_plugin(name: str, principal: Principal = Depends(admin_only)) -> PluginOut:  # noqa: B008
        return _pin_route(name, principal, pinned=False)

    @router.post("/plugins/{name}/update")
    async def update_plugin(
        name: str,
        principal: Principal = Depends(admin_only),  # noqa: B008
    ) -> PluginOut:
        key, dist = _installed_dist(name)
        layout = guard.Layout.of(data_dir())
        fetcher = index_fetcher_for()
        try:
            await lifecycle.update(
                engine(),
                repos(),
                layout,
                key,
                dist,
                fetcher,
                audit=_lifecycle_audit(principal, "plugin.update", key),
            )
        except (
            lifecycle.PluginLifecycleError,
            PluginIndexError,
            guard.PluginInstallError,
        ) as exc:
            code = getattr(exc, "code", "update_failed")
            _audit(principal, "plugin.update_refused", f"{key}:{code}")
            raise ProblemError("plugin.update_refused", detail=exc.detail) from None
        finally:
            await fetcher.aclose()
        # a new version: the word given for the old one lapses
        attestations().sync_installed(installed_for())
        # the guard's view speaks the distribution name, the route was addressed by the key
        return _one(layout, dist)

    @router.post("/plugins/{name}/rollback")
    def rollback_plugin(name: str, principal: Principal = Depends(_ROLLBACK)) -> PluginOut:  # noqa: B008
        key, dist = _installed_dist(name)
        layout = guard.Layout.of(data_dir())
        try:
            lifecycle.rollback(
                engine(),
                repos(),
                layout,
                key,
                dist,
                audit=_lifecycle_audit(principal, "plugin.rollback", key),
            )
        except (lifecycle.PluginLifecycleError, guard.PluginInstallError) as exc:
            code = getattr(exc, "code", "rollback_failed")
            _audit(principal, "plugin.rollback_refused", f"{key}:{code}")
            raise ProblemError("plugin.rollback_refused", detail=exc.detail) from None
        # another version: the word given for the other one lapses
        attestations().sync_installed(installed_for())
        # the guard's view speaks the distribution name, the route was addressed by the key
        return _one(layout, dist)

    return router
