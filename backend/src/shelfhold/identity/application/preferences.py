"""The appearance of a person: theme and colour scheme (EF-UX-012).

`theme` is null until the person chooses one; the instance default shows through (theme A when
the instance has none). Only the themes that ship can be chosen."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

DEFAULT_THEME = "bibliotheque"  # theme A; B, C and D arrive in M4
DELIVERED_THEMES = (DEFAULT_THEME,)
COLOR_SCHEMES = ("auto", "light", "dark")


@dataclass(frozen=True)
class Preferences:
    theme: str  # in effect: the choice, else the instance default
    chosen_theme: str | None
    color_scheme: str


class PreferencesStore(Protocol):
    def read(self, user_id: str) -> tuple[str | None, str]:
        """`(theme, color_scheme)` of the user row."""
        ...

    def instance_default_theme(self) -> str | None: ...

    def update(self, user_id: str, changes: Mapping[str, str | None]) -> None: ...


class PreferencesService:
    def __init__(self, store: PreferencesStore) -> None:
        self._store = store

    def get(self, user_id: str) -> Preferences:
        chosen, scheme = self._store.read(user_id)
        default = self._store.instance_default_theme()
        fallback = default if default in DELIVERED_THEMES else DEFAULT_THEME
        return Preferences(theme=chosen or fallback, chosen_theme=chosen, color_scheme=scheme)

    def change(self, user_id: str, changes: Mapping[str, str | None]) -> Preferences:
        """Only `theme` and `color_scheme`, only values that ship; anything else is refused."""
        if not set(changes) <= {"theme", "color_scheme"}:
            raise ValueError("only the theme and the colour scheme can be changed")
        theme = changes.get("theme")
        if "theme" in changes and theme is not None and theme not in DELIVERED_THEMES:
            raise ValueError("this theme is not delivered")
        if "color_scheme" in changes and changes["color_scheme"] not in COLOR_SCHEMES:
            raise ValueError("unknown colour scheme")
        if changes:
            self._store.update(user_id, changes)
        return self.get(user_id)
