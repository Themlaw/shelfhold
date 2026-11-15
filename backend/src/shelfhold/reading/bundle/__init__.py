"""The block of a series: a virtual stored ZIP served on OPDS (M4 L49a, ADR-0053)."""

from shelfhold.reading.bundle.errors import (
    BundleBusyError,
    BundleChangedError,
    BundleGoneError,
    BundleNotFoundError,
)
from shelfhold.reading.bundle.limits import BundleLimits
from shelfhold.reading.bundle.model import DEFAULT_MEDIA_TYPE, BundleDownload, BundleOffer
from shelfhold.reading.bundle.service import SeriesBundles, prune_versions

__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "BundleBusyError",
    "BundleChangedError",
    "BundleDownload",
    "BundleGoneError",
    "BundleLimits",
    "BundleNotFoundError",
    "BundleOffer",
    "SeriesBundles",
    "prune_versions",
]
