"""Aggregation of the per-context error declarations (`<ctx>/errors.py`, EF-UX-003)."""

from functools import cache
from types import ModuleType

from shelfhold.acquisition import errors as acquisition_errors
from shelfhold.error_catalog import ErrorCode
from shelfhold.events import errors as events_errors
from shelfhold.export import errors as export_errors
from shelfhold.identity import errors as identity_errors
from shelfhold.interfaces.http_api import errors as http_errors
from shelfhold.library import errors as library_errors

# A new context adds its `errors` module here; a test fails if one is declared but not listed.
_MODULES: tuple[ModuleType, ...] = (
    acquisition_errors,
    events_errors,
    export_errors,
    identity_errors,
    http_errors,
    library_errors,
)


def error_code_modules() -> tuple[str, ...]:
    return tuple(module.__name__ for module in _MODULES)


@cache
def _by_code() -> dict[str, ErrorCode]:
    declared: dict[str, ErrorCode] = {}
    for module in _MODULES:
        for spec in module.CODES:
            if spec.code in declared:
                raise ValueError(f"duplicate_error_code:{spec.code}")
            declared[spec.code] = spec
    return declared


def all_error_codes() -> tuple[ErrorCode, ...]:
    return tuple(_by_code().values())


def error_code(code: str) -> ErrorCode:
    """The catalog entry; an unknown code is a programming error, not a client error."""
    return _by_code()[code]
