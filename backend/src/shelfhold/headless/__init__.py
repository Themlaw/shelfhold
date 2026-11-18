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
    SyncProgressSet,
    TargetAdded,
)
from shelfhold.headless.desired import DesiredConfig, parse_desired
from shelfhold.headless.errors import (
    AdminRiskNotAccepted,
    ConfigFileError,
    DestinationCannotSync,
    DestinationNotWritable,
    HeadlessError,
    InvalidConfigValue,
    ReadOnlyConfigKey,
    ShelfConflict,
    SourceAuthModeUnsupported,
    SourceUpdateFiltersBlocking,
    TargetConflict,
    TargetExchangeFailed,
    TargetLocalSourceUnknown,
    TargetRootOverlapsLocalSource,
    UnknownConfigKey,
    UnknownTarget,
)
from shelfhold.headless.ports import (
    ExchangedKey,
    KavitaKeyExchange,
    KomgaProbe,
    KomgaProbed,
    SuwayomiProbe,
)
from shelfhold.headless.urls import PublicUrlMissing, absolute_url

__all__ = [
    "AdminRiskNotAccepted",
    "ConfigFileError",
    "ConfigValue",
    "DesiredConfig",
    "DestinationCannotSync",
    "DestinationNotWritable",
    "ExchangedKey",
    "HeadlessAdmin",
    "HeadlessError",
    "InvalidConfigValue",
    "KavitaKeyExchange",
    "KomgaProbe",
    "KomgaProbed",
    "Plan",
    "PlanChange",
    "PublicUrlMissing",
    "ReadOnlyConfigKey",
    "ShelfConflict",
    "ShelfCreated",
    "SourceAdded",
    "SourceAuthModeUnsupported",
    "SourceUpdateFiltersBlocking",
    "SuwayomiProbe",
    "SyncProgressSet",
    "TargetAdded",
    "TargetConflict",
    "TargetExchangeFailed",
    "TargetLocalSourceUnknown",
    "TargetRootOverlapsLocalSource",
    "UnknownConfigKey",
    "UnknownTarget",
    "absolute_url",
    "parse_desired",
]
