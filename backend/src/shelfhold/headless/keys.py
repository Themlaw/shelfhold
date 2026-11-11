"""The settable configuration keys of M1a, with their default and their validation (EF-INT-024).

`config.toml` read at start-up and the declarative file of `config apply` use the same keys. The
documented default of each key is here; `docs/reference/config.md` is generated from it.
"""

from collections.abc import Callable
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
    "KEYS",
    "MAINTENANCE_WINDOW",
    "PATH_MAP_PREFIX",
    "SECRET_KEYS",
    "ConfigKey",
    "effective_check_mode",
    "warning_of",
]

PATH_MAP_PREFIX: Final = "path_map."
MAINTENANCE_WINDOW: Final = "03:00-07:00 Europe/Paris"  # fixed in M1a (ENF-OPS-014)
CHECK_MODES: Final = ("always_on", "piloted")


@dataclass(frozen=True)
class ConfigKey:
    name: str
    default: object
    help: str
    validate: Callable[[str], str] | None = None
    readonly: bool = False
    secret: bool = False  # its value is a secret: never on a command line, never shown
    default_doc: str | None = None  # how the generated reference words a computed default


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
                  _public_url),
        ConfigKey("source.check_mode", "always_on",
                  "How new chapters are looked for: always_on or piloted (EF-SRC-025).",
                  _check_mode,
                  default_doc="`always_on`, ou `piloted` quand un réveil (Sablier) est configuré"),
        ConfigKey("export.missing_floor", FLOOR_MISSING,
                  "Sentinel: files missing in a shelf destination before the share counts "
                  "(EF-EXP-029).", _whole("export.missing_floor")),
        ConfigKey("export.missing_ratio", RATIO_MISSING,
                  "Sentinel: share of missing files, above which (with the floor) exports, "
                  "removals and scans are blocked (EF-EXP-029).", _share),
        ConfigKey("export.missing_ceiling", CEILING_MISSING,
                  "Sentinel: more missing files than this block whatever the share "
                  "(EF-EXP-029).", _whole("export.missing_ceiling")),
        ConfigKey("instance.jurisdiction", default_jurisdiction(),
                  "Country whose copyright law drives the public-domain notices; the default "
                  "is deduced from the locale (EF-SRC-024).", _jurisdiction,
                  default_doc="déduit de la langue du système"),
        ConfigKey("maintenance.window", MAINTENANCE_WINDOW,
                  "The default maintenance window (ENF-OPS-014), read only here: it is set by "
                  "SHELFHOLD_MAINTENANCE_WINDOW_START, _END and SHELFHOLD_TIMEZONE.",
                  readonly=True),
        ConfigKey("notify.urls", [], "Notification channels, name=url; the URL is sealed.",
                  secret=True),
    )
}  # fmt: skip
SECRET_KEYS: Final = frozenset(name for name, key in KEYS.items() if key.secret)


def warning_of(key: str, value: object) -> str | None:
    """A value that is accepted but does not act: said at `config set` and `config get`. None
    today: the piloted check of EF-SRC-025 acts since M1b (L21)."""
    return None
