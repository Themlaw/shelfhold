"""The OPDS feed of Shelfhold for an e-reader (M4 L48, ADR-0053 §1, EF-INT-007).

Behind a device token only (`device_auth.require_device_basic`): HTTP Basic, over TLS, for the
profile the token was issued for. See `routes` for the doors, `feed` for the Atom document,
`links` for what a link may be, and `bundle` for the extension point of the block of a series.
"""

from shelfhold.interfaces.http_api.opds.bundle import (
    BundleDownload,
    BundleOffer,
    SeriesBundleProvider,
)
from shelfhold.interfaces.http_api.opds.routes import build_opds_router

__all__ = ["BundleDownload", "BundleOffer", "SeriesBundleProvider", "build_opds_router"]
