"""`/api/v1/me/preferences`: the theme and the colour scheme of the signed-in person (EF-UX-012).

They live on the server, on the `user` row, so that another device shows the same UI. A session
only: a key or a token is a program, it has no appearance. `theme` is null until the person
chooses one, and the instance default (`app_setting` `ui.default_theme`, theme A otherwise) shows
through. Only the themes that ship can be chosen; nothing else on the user is writable here.
"""

from collections.abc import Callable
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from shelfhold.identity.public import Principal
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.interfaces.http_api.auth import require_session

__all__ = ["DEFAULT_THEME", "DEFAULT_THEME_SETTING", "build_identity_router"]

DEFAULT_THEME = "bibliotheque"  # theme A; B, C and D arrive in M4
DEFAULT_THEME_SETTING = "ui.default_theme"


class PreferencesOut(BaseModel):
    theme: str  # the theme in effect: the choice, else the instance default
    chosen_theme: str | None  # null: the person has not chosen, the default applies
    color_scheme: Literal["auto", "light", "dark"]


class PreferencesIn(BaseModel):
    """Only what is named is changed; `theme: null` goes back to the instance default."""

    model_config = ConfigDict(extra="forbid")

    theme: Literal["bibliotheque"] | None = None
    color_scheme: Literal["auto", "light", "dark"] = "auto"


def _instance_default(connection: sa.Connection) -> str:
    row = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == DEFAULT_THEME_SETTING
        )
    ).scalar_one_or_none()
    return row if row in identity.THEMES else DEFAULT_THEME


def _read(connection: sa.Connection, user_id: str) -> PreferencesOut:
    user = identity.user
    chosen, scheme = connection.execute(
        sa.select(user.c.theme, user.c.color_scheme).where(user.c.id == user_id)
    ).one()
    return PreferencesOut(
        theme=chosen or _instance_default(connection), chosen_theme=chosen, color_scheme=scheme
    )


def build_identity_router(engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me")

    @router.get("/preferences", response_model=PreferencesOut)
    def get_preferences(
        principal: Annotated[Principal, Depends(require_session)],
    ) -> PreferencesOut:
        with engine().connect() as connection:
            return _read(connection, principal.user_id)

    @router.patch("/preferences", response_model=PreferencesOut)
    def patch_preferences(
        body: PreferencesIn, principal: Annotated[Principal, Depends(require_session)]
    ) -> PreferencesOut:
        changes = {name: getattr(body, name) for name in body.model_fields_set}
        with engine().begin() as connection:
            if changes:
                user = identity.user
                connection.execute(
                    sa.update(user).where(user.c.id == principal.user_id).values(**changes)
                )
            return _read(connection, principal.user_id)

    return router
