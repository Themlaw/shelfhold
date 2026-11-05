"""Headless configuration of an instance (EF-INT-024): no UI, no SQL.

`HeadlessAdmin` is the one facade behind `shelfhold config|target|source|shelf`, the socket
methods of the local CLI and the routes `/api/v1/config|targets|sources`.
"""

from shelfhold.headless.admin import (
    ConfigValue,
    HeadlessAdmin,
    Plan,
    PlanChange,
    ShelfCreated,
    SourceAdded,
    TargetAdded,
)
from shelfhold.headless.desired import DesiredConfig, parse_desired
from shelfhold.headless.errors import (
    AdminRiskNotAccepted,
    ConfigFileError,
    HeadlessError,
    InvalidConfigValue,
    ReadOnlyConfigKey,
    ShelfConflict,
    SourceAuthModeUnsupported,
    TargetConflict,
    TargetExchangeFailed,
    UnknownConfigKey,
    UnknownTarget,
)
from shelfhold.headless.ports import ExchangedKey, KavitaKeyExchange, SuwayomiProbe
from shelfhold.headless.urls import PublicUrlMissing, absolute_url

__all__ = [
    "AdminRiskNotAccepted",
    "ConfigFileError",
    "ConfigValue",
    "DesiredConfig",
    "ExchangedKey",
    "HeadlessAdmin",
    "HeadlessError",
    "InvalidConfigValue",
    "KavitaKeyExchange",
    "Plan",
    "PlanChange",
    "PublicUrlMissing",
    "ReadOnlyConfigKey",
    "ShelfConflict",
    "ShelfCreated",
    "SourceAdded",
    "SourceAuthModeUnsupported",
    "SuwayomiProbe",
    "TargetAdded",
    "TargetConflict",
    "TargetExchangeFailed",
    "UnknownConfigKey",
    "UnknownTarget",
    "absolute_url",
    "parse_desired",
]
