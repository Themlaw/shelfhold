"""The Kavita destination: allow-listed routes, version range, degraded modes (EF-EXP-009)."""

from shelfhold.export.targets.kavita.client import CallResult, KavitaClient
from shelfhold.export.targets.kavita.credentials import (
    AdminKey,
    load_admin_key,
    store_admin_key,
)
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.export.targets.kavita.dto import (
    HIDDEN_DIR_PATTERNS,
    LibraryType,
    LibraryUpdate,
    ParseResult,
)
from shelfhold.export.targets.kavita.errors import (
    ConfirmationRequired,
    ProtocolError,
    RouteMissing,
    RouteNotAllowed,
    TargetDegraded,
    TargetError,
    TargetForbidden,
    TargetUnreachable,
)
from shelfhold.export.targets.kavita.routes import ALLOWED_ROUTES, SETUP_ROUTES, Route

__all__ = [
    "ALLOWED_ROUTES",
    "HIDDEN_DIR_PATTERNS",
    "SETUP_ROUTES",
    "AdminKey",
    "CallResult",
    "ConfirmationRequired",
    "KavitaClient",
    "KavitaDestination",
    "LibraryType",
    "LibraryUpdate",
    "ParseResult",
    "ProtocolError",
    "Route",
    "RouteMissing",
    "RouteNotAllowed",
    "TargetDegraded",
    "TargetError",
    "TargetForbidden",
    "TargetUnreachable",
    "load_admin_key",
    "store_admin_key",
]
