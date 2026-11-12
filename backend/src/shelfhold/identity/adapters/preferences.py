"""SQL store of the appearance preferences (`user.theme`, `user.color_scheme`)."""

from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.schema import identity, platform

DEFAULT_THEME_SETTING = "ui.default_theme"


class SqlPreferencesStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(self, user_id: str) -> tuple[str | None, str]:
        user = identity.user
        with self._engine.connect() as connection:
            theme, scheme = connection.execute(
                sa.select(user.c.theme, user.c.color_scheme).where(user.c.id == user_id)
            ).one()
        return theme, scheme

    def instance_default_theme(self) -> str | None:
        setting = platform.app_setting
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(setting.c.value).where(setting.c.key == DEFAULT_THEME_SETTING)
            ).scalar_one_or_none()

    def update(self, user_id: str, changes: Mapping[str, str | None]) -> None:
        user = identity.user
        with self._engine.begin() as connection:
            connection.execute(sa.update(user).where(user.c.id == user_id).values(**changes))
