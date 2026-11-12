"""SQLAlchemy Core schema: one MetaData, tables grouped by area (PLAN-M1a A17)."""

from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    coherence,
    export,
    identity,
    platform,
    reading,
    replica,
    storage,
)
from shelfhold.infrastructure.schema._base import UtcDateTime, metadata

__all__ = [
    "UtcDateTime",
    "acquisition",
    "catalog",
    "coherence",
    "export",
    "identity",
    "metadata",
    "platform",
    "reading",
    "replica",
    "storage",
]
