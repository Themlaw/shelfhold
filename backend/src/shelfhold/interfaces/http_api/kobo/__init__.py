"""The Kobo sync emulation of Shelfhold (M6 L17a, EF-INT-012, D20).

Behind a device token of kind `kobo` only (`device_auth.require_device_kobo`): the credential is
the path segment of the sync address, over TLS, for the profile the token was issued for. See
`routes` for the doors and `reading.application.kobo_sync` for what a sync serves.
"""

from shelfhold.interfaces.http_api.kobo.routes import build_kobo_router

__all__ = ["build_kobo_router"]
