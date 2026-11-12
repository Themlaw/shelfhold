"""`/api/v1/me/preferences`: the theme and the colour scheme of the signed-in person (EF-UX-012).

They live on the server, on the `user` row, so that another device shows the same UI. A session
only: a key or a token is a program, it has no appearance. The rules are the application
service's; this module only carries them over HTTP. Nothing else on the user is writable here.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from shelfhold.identity.application.preferences import Preferences, PreferencesService
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_session

__all__ = ["build_identity_router"]


class PreferencesOut(BaseModel):
    theme: str  # the theme in effect: the choice, else the instance default
    chosen_theme: str | None  # null: the person has not chosen, the default applies
    color_scheme: Literal["auto", "light", "dark"]


class MeOut(BaseModel):
    """Who the signed-in person is, including their role (EF-UX-005): the family/admin
    navigation of the UI is derived from it."""

    id: str
    username: str
    role: str


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

    @router.get("", response_model=MeOut)
    def me(principal: Annotated[Principal, Depends(require_session)]) -> MeOut:
        return MeOut(id=principal.user_id, username=principal.username, role=principal.role)

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
