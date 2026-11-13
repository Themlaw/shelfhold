"""The settable configuration keys of M1a, with their default and their validation (EF-INT-024).

`config.toml` read at start-up and the declarative file of `config apply` use the same keys. The
documented default of each key is here; `docs/reference/config.md` is generated from it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from shelfhold.export.application.root_bootstrap import default_jurisdiction
from shelfhold.export.domain.reconcile import (
    CEILING_MISSING,
    FLOOR_MISSING,
    MAX_CEILING,
    MAX_RATIO,
    RATIO_MISSING,
)
from shelfhold.headless.errors import InvalidConfigValue
from shelfhold.headless.urls import validate_public_url

__all__ = [
    "ESSENTIAL_MAX",
    "KEYS",
    "LEVELS",
    "MAINTENANCE_WINDOW",
    "PATH_MAP_LEVEL",
    "PATH_MAP_PREFIX",
    "SECRET_KEYS",
    "ConfigKey",
    "check_levels",
    "effective_check_mode",
    "warning_of",
]

PATH_MAP_PREFIX: Final = "path_map."
MAINTENANCE_WINDOW: Final = "03:00-07:00 Europe/Paris"  # fixed in M1a (ENF-OPS-014)
CHECK_MODES: Final = ("always_on", "piloted")
LEVELS: Final = ("essential", "advanced", "expert")  # the screen's levels (EF-UX-009)
ESSENTIAL_MAX: Final = 15
PATH_MAP_LEVEL: Final = "advanced"  # the folder mapping of a target: one row per target


@dataclass(frozen=True)
class ConfigKey:
    name: str
    default: object
    help: str
    validate: Callable[[str], str] | None = None
    readonly: bool = False
    secret: bool = False  # its value is a secret: never on a command line, never shown
    default_doc: str | None = None  # how the generated reference words a computed default
    level: str = "expert"  # the screen's level: the least visible unless the key says otherwise


def _public_url(value: str) -> str:
    try:
        return validate_public_url(value)
    except ValueError as error:
        raise InvalidConfigValue(str(error)) from None


def effective_check_mode(stored: str | None, *, wake_configured: bool) -> str:
    """What the follow runs in. An explicit `source.check_mode` wins; without one the mode is
    `piloted` when a WakeHook is configured and `always_on` otherwise (EF-SRC-025, D8)."""
    if stored in CHECK_MODES:
        return str(stored)
    return "piloted" if wake_configured else "always_on"


def _check_mode(value: str) -> str:
    if value not in CHECK_MODES:
        raise InvalidConfigValue(f"source.check_mode is one of {', '.join(CHECK_MODES)}")
    return value


def _whole(name: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not value.strip().isdigit() or not 1 <= int(value) <= MAX_CEILING:
            raise InvalidConfigValue(f"{name} is a whole number from 1 to {MAX_CEILING}")
        return str(int(value))

    return check


def _share(value: str) -> str:
    try:
        share = float(value)
    except ValueError:
        raise InvalidConfigValue("export.missing_ratio is a share, e.g. 0.05") from None
    if not 0 < share <= MAX_RATIO:
        raise InvalidConfigValue(f"export.missing_ratio is above 0 and at most {MAX_RATIO}")
    return str(share)


def _jurisdiction(value: str) -> str:
    clean = value.strip().upper()
    if len(clean) != 2 or not clean.isalpha():
        raise InvalidConfigValue("instance.jurisdiction is a two-letter country code, e.g. FR")
    return clean


KEYS: Final = {
    key.name: key
    for key in (
        ConfigKey("public_url", None, "Public address; every absolute link is built from it.",
                  _public_url, level="essential"),
        ConfigKey("source.check_mode", "always_on",
                  "How new chapters are looked for: always_on or piloted (EF-SRC-025).",
                  _check_mode,
                  level="essential",
                  default_doc="`always_on`, ou `piloted` quand un réveil (Sablier) est configuré"),
        ConfigKey("export.missing_floor", FLOOR_MISSING,
                  "Sentinel: files missing in a shelf destination before the share counts "
                  "(EF-EXP-029).", _whole("export.missing_floor"), level="advanced"),
        ConfigKey("export.missing_ratio", RATIO_MISSING,
                  "Sentinel: share of missing files, above which (with the floor) exports, "
                  "removals and scans are blocked (EF-EXP-029).", _share, level="advanced"),
        ConfigKey("export.missing_ceiling", CEILING_MISSING,
                  "Sentinel: more missing files than this block whatever the share "
                  "(EF-EXP-029).", _whole("export.missing_ceiling"), level="advanced"),
        ConfigKey("instance.jurisdiction", default_jurisdiction(),
                  "Country whose copyright law drives the public-domain notices; the default "
                  "is deduced from the locale (EF-SRC-024).", _jurisdiction,
                  level="essential", default_doc="déduit de la langue du système"),
        ConfigKey("maintenance.window", MAINTENANCE_WINDOW,
                  "The default maintenance window (ENF-OPS-014), read only here: it is set by "
                  "SHELFHOLD_MAINTENANCE_WINDOW_START, _END and SHELFHOLD_TIMEZONE.",
                  readonly=True, level="essential"),
        ConfigKey("notify.urls", [], "Notification channels, name=url; the URL is sealed.",
                  secret=True, level="expert"),
    )
}  # fmt: skip


def check_levels(keys: Mapping[str, ConfigKey]) -> None:
    """Every key has a known level and the essential screen stays short (EF-UX-009)."""
    for name, key in keys.items():
        if key.level not in LEVELS:
            raise ValueError(f"{name}: level {key.level!r} is none of {', '.join(LEVELS)}")
    essential = [name for name, key in keys.items() if key.level == "essential"]
    if len(essential) > ESSENTIAL_MAX:
        raise ValueError(f"{len(essential)} essential settings: at most {ESSENTIAL_MAX}")


check_levels(KEYS)
SECRET_KEYS: Final = frozenset(name for name, key in KEYS.items() if key.secret)


def warning_of(key: str, value: object) -> str | None:
    """A value that is accepted but does not act: said at `config set` and `config get`. None
    today: the piloted check of EF-SRC-025 acts since M1b (L21)."""
    return None
