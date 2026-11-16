"""Suwayomi adapter: the only code that knows Suwayomi (EF-SRC-033)."""

from shelfhold.plugin_host.adapters.meta.suwayomi.jwt import suwayomi_jwt_fetcher
from shelfhold.plugin_host.adapters.meta.suwayomi.read_state import SuwayomiReadState
from shelfhold.plugin_host.adapters.meta.suwayomi.service import SuwayomiService

__all__ = ["SuwayomiReadState", "SuwayomiService", "suwayomi_jwt_fetcher"]
