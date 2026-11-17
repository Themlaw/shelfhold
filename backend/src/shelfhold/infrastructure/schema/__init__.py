"""SQLAlchemy Core schema: one MetaData, tables grouped by area (PLAN-M1a A17)."""

from shelfhold.infrastructure.schema import (
    acquisition,
    bundle,
    catalog,
    coherence,
    export,
    identity,
    platform,
    reading,
    replica,
    storage,
    webhooks,
)
from shelfhold.infrastructure.schema._base import UtcDateTime, metadata

__all__ = [
    "UtcDateTime",
    "acquisition",
    "bundle",
    "catalog",
    "coherence",
    "export",
    "identity",
    "metadata",
    "platform",
    "reading",
    "replica",
    "storage",
    "webhooks",
]
