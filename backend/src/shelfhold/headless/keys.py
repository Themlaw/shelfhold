"""The settable configuration keys of M1a, with their default and their validation (EF-INT-024).

`config.toml` read at start-up and the declarative file of `config apply` use the same keys. The
documented default of each key is here; `docs/reference/config.md` is generated from it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from shelfhold.export.application.root_bootstrap import default_jurisdiction
from shelfhold.headless.errors import InvalidConfigValue
from shelfhold.headless.urls import validate_public_url

__all__ = ["KEYS", "MAINTENANCE_WINDOW", "PATH_MAP_PREFIX", "SECRET_KEYS", "ConfigKey"]

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


def _public_url(value: str) -> str:
    try:
        return validate_public_url(value)
    except ValueError as error:
        raise InvalidConfigValue(str(error)) from None


def _check_mode(value: str) -> str:
    if value not in CHECK_MODES:
        raise InvalidConfigValue(f"source.check_mode is one of {', '.join(CHECK_MODES)}")
    return value


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
                  _check_mode),
        ConfigKey("instance.jurisdiction", default_jurisdiction(),
                  "Country whose copyright law drives the public-domain notices; the default "
                  "is deduced from the locale (EF-SRC-024).", _jurisdiction),
        ConfigKey("maintenance.window", MAINTENANCE_WINDOW,
                  "The fixed maintenance window (ENF-OPS-014); read only in M1a.", readonly=True),
        ConfigKey("notify.urls", [], "Notification channels, name=url; the URL is sealed.",
                  secret=True),
    )
}  # fmt: skip
SECRET_KEYS: Final = frozenset(name for name, key in KEYS.items() if key.secret)
