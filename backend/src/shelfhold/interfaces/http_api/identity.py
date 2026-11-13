"""`/api/v1/me/preferences`: the theme and the colour scheme of the signed-in person (EF-UX-012).

They live on the server, on the `user` row, so that another device shows the same UI. A session
only: a key or a token is a program, it has no appearance. The rules are the application
service's; this module only carries them over HTTP. Nothing else on the user is writable here.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from shelfhold.identity.application.notification_prefs import (
    NotificationPrefs,
    NotificationPrefsError,
    NotificationPrefsService,
)
from shelfhold.identity.application.preferences import Preferences, PreferencesService
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_session
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_identity_router", "build_notification_prefs_router"]


class PreferencesOut(BaseModel):
    theme: str  # the theme in effect: the choice, else the instance default
    chosen_theme: str | None  # null: the person has not chosen, the default applies
    color_scheme: Literal["auto", "light", "dark"]


class PreferencesIn(BaseModel):
    """Only what is named is changed; `theme: null` goes back to the instance default."""

    model_config = ConfigDict(extra="forbid")

    theme: Literal["bibliotheque"] | None = None
    color_scheme: Literal["auto", "light", "dark"] = "auto"


def _out(preferences: Preferences) -> PreferencesOut:
    return PreferencesOut(
        theme=preferences.theme,
        chosen_theme=preferences.chosen_theme,
        color_scheme=preferences.color_scheme,  # type: ignore[arg-type]
    )


def build_identity_router(service: Callable[[], PreferencesService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me")

    @router.get("/preferences", response_model=PreferencesOut)
    def get_preferences(
        principal: Annotated[Principal, Depends(require_session)],
    ) -> PreferencesOut:
        return _out(service().get(principal.user_id))

    @router.patch("/preferences", response_model=PreferencesOut)
    def patch_preferences(
        body: PreferencesIn, principal: Annotated[Principal, Depends(require_session)]
    ) -> PreferencesOut:
        changes = {name: getattr(body, name) for name in body.model_fields_set}
        return _out(service().change(principal.user_id, changes))

    return router


class NotificationPrefsOut(BaseModel):
    notify_mode: Literal["digest", "immediate"]
    quiet_start: str | None
    quiet_end: str | None
    timezone: str
    effective_quiet: list[str]  # the hours in force: the profile's own, else the default


class NotificationPrefsIn(BaseModel):
    """The whole of it, replaced at once. Nothing names a person: the caller's row is the one."""

    model_config = ConfigDict(extra="forbid")

    notify_mode: str
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str  # required: a partial body must not reset the zone in silence


def _prefs_out(prefs: NotificationPrefs) -> NotificationPrefsOut:
    return NotificationPrefsOut(
        notify_mode=prefs.notify_mode,  # type: ignore[arg-type]
        quiet_start=prefs.quiet_start, quiet_end=prefs.quiet_end, timezone=prefs.timezone,
        effective_quiet=list(prefs.effective_quiet),
    )  # fmt: skip


def build_notification_prefs_router(
    service: Callable[[], NotificationPrefsService],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["me"])

    @router.get("/notifications", response_model=NotificationPrefsOut)
    def get_notifications(
        principal: Annotated[Principal, Depends(require_session)],
    ) -> NotificationPrefsOut:
        return _prefs_out(_run(lambda: service().get(principal.user_id)))

    @router.put("/notifications", response_model=NotificationPrefsOut)
    def put_notifications(
        body: NotificationPrefsIn, principal: Annotated[Principal, Depends(require_session)]
    ) -> NotificationPrefsOut:
        return _prefs_out(_run(lambda: service().set(principal.user_id, body.model_dump())))

    return router


def _run[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except NotificationPrefsError as error:
        if error.code == "not_found":
            raise ProblemError("not_found") from None
        raise ProblemError(
            "notification_prefs_refused", extensions={"reason": error.code}
        ) from None
