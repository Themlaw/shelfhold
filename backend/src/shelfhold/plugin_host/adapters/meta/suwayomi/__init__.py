"""Suwayomi adapter: the only code that knows Suwayomi (EF-SRC-033)."""

from shelfhold.plugin_host.adapters.meta.suwayomi.jwt import suwayomi_jwt_fetcher
from shelfhold.plugin_host.adapters.meta.suwayomi.service import SuwayomiService

__all__ = ["SuwayomiService", "suwayomi_jwt_fetcher"]
