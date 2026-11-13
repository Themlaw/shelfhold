"""How a person wants to be told (EF-INT-021): the mode, the quiet hours, the time zone.

They are the person's own, on the `user` row; nothing here takes another person's identifier. The
digest hour is not a setting (it is 08:00 local time, D16), and neither is the list of what is
immediate: the profile chooses a mode and when to be left alone, nothing more.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from zoneinfo import ZoneInfo, available_timezones

MODES: Final = ("digest", "immediate")
DEFAULT_QUIET: Final = ("21:00", "07:00")
_CLOCK: Final = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class NotificationPrefsError(ValueError):
    """A refusal with a stable code; the text never carries what the caller sent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NotificationPrefs:
    notify_mode: str
    quiet_start: str | None
    quiet_end: str | None
    timezone: str

    @property
    def effective_quiet(self) -> tuple[str, str]:
        if self.quiet_start and self.quiet_end:
            return self.quiet_start, self.quiet_end
        return DEFAULT_QUIET


class NotificationPrefsStore(Protocol):
    def read(self, user_id: str) -> NotificationPrefs | None: ...

    def write(self, user_id: str, prefs: NotificationPrefs) -> None: ...


_ZONES: frozenset[str] | None = None


def _known_zone(name: object) -> bool:
    global _ZONES
    if not isinstance(name, str) or not name or len(name) > 64:
        return False
    if _ZONES is None:
        _ZONES = frozenset(available_timezones())
    if name not in _ZONES:
        return False
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


def check(values: Mapping[str, object]) -> NotificationPrefs:
    mode, start, end = values.get("notify_mode"), values.get("quiet_start"), values.get("quiet_end")
    zone = values.get("timezone")
    if mode not in MODES:
        raise NotificationPrefsError("invalid_notify_mode")
    if (start is None) != (end is None):
        raise NotificationPrefsError("quiet_hours_both_or_none")
    for one in (start, end):
        if one is not None and not (isinstance(one, str) and _CLOCK.fullmatch(one)):
            raise NotificationPrefsError("invalid_quiet_hours")
    if not _known_zone(zone):
        raise NotificationPrefsError("invalid_timezone")
    return NotificationPrefs(str(mode), start, end, str(zone))  # type: ignore[arg-type]


class NotificationPrefsService:
    def __init__(self, store: NotificationPrefsStore) -> None:
        self._store = store

    def get(self, user_id: str) -> NotificationPrefs:
        prefs = self._store.read(user_id)
        if prefs is None:
            raise NotificationPrefsError("not_found")
        return prefs

    def set(self, user_id: str, values: Mapping[str, object]) -> NotificationPrefs:
        prefs = check(values)
        self.get(user_id)  # a profile that is gone has nothing to change
        self._store.write(user_id, prefs)
        return prefs
