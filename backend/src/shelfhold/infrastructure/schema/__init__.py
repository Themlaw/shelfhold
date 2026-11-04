"""SQLAlchemy Core schema: one MetaData, tables grouped by area (PLAN-M1a A17)."""

from shelfhold.infrastructure.schema import (
    acquisition,
    catalog,
    coherence,
    identity,
    platform,
)
from shelfhold.infrastructure.schema._base import UtcDateTime, metadata

__all__ = [
    "UtcDateTime",
    "acquisition",
    "catalog",
    "coherence",
    "identity",
    "metadata",
    "platform",
]
