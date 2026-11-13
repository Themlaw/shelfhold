"""SQL store of the appearance preferences (`user.theme`, `user.color_scheme`)."""

from collections.abc import Mapping

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.application.notification_prefs import NotificationPrefs
from shelfhold.infrastructure.schema import identity, platform

DEFAULT_THEME_SETTING = "ui.default_theme"


class SqlPreferencesStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(self, user_id: str) -> tuple[str | None, str, str]:
        user = identity.user
        with self._engine.connect() as connection:
            theme, scheme, locale = connection.execute(
                sa.select(user.c.theme, user.c.color_scheme, user.c.locale).where(
                    user.c.id == user_id
                )
            ).one()
        return theme, scheme, locale

    def instance_default_theme(self) -> str | None:
        setting = platform.app_setting
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(setting.c.value).where(setting.c.key == DEFAULT_THEME_SETTING)
            ).scalar_one_or_none()

    def update(self, user_id: str, changes: Mapping[str, str | None]) -> None:
        # the application names the choice `language`, the column of the user row is `locale`
        values = {
            "locale" if name == "language" else name: value for name, value in changes.items()
        }
        user = identity.user
        with self._engine.begin() as connection:
            connection.execute(sa.update(user).where(user.c.id == user_id).values(**values))


class SqlNotificationPrefsStore:
    """The mode, the quiet hours and the zone of one `user` row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(self, user_id: str) -> NotificationPrefs | None:
        user = identity.user
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    user.c.notify_mode, user.c.quiet_start, user.c.quiet_end, user.c.timezone
                ).where(user.c.id == user_id)
            ).first()
        return None if row is None else NotificationPrefs(*row)

    def write(self, user_id: str, prefs: NotificationPrefs) -> None:
        user = identity.user
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(user)
                .where(user.c.id == user_id)
                .values(
                    notify_mode=prefs.notify_mode, quiet_start=prefs.quiet_start,
                    quiet_end=prefs.quiet_end, timezone=prefs.timezone,
                )
            )  # fmt: skip
