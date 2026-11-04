"""Message catalogs (ENF-I18N-001): French only in M0, English added before 1.0."""

import tomllib
from functools import cache
from importlib import resources
from typing import Any

DEFAULT_LANGUAGE = "fr"


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, dotted + "."))
        else:
            flat[dotted] = str(value)
    return flat


@cache
def load_catalog(language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    text = resources.files("shelfhold").joinpath("locales", f"{language}.toml").read_text("utf-8")
    return _flatten(tomllib.loads(text))


def t(key: str, **params: object) -> str:
    """Return the localized message for `key`; an unknown key raises KeyError."""
    return load_catalog()[key].format(**params)
