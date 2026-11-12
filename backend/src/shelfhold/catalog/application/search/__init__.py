"""Catalogue search (M3). The only reader of the FTS tables, by the contract `fts-sole-reader`;
the search lot (M3 L17) fills this package and `infrastructure.schema.search`."""

from shelfhold.catalog.application.search.capability import (
    SearchCapability,
    ensure_tables,
    search_capability,
)
from shelfhold.catalog.application.search.index import index_work, rebuild, remove_works, sync
from shelfhold.catalog.application.search.service import (
    SearchHit,
    SearchPage,
    search_page,
)

__all__ = [
    "SearchCapability",
    "SearchHit",
    "SearchPage",
    "ensure_tables",
    "index_work",
    "rebuild",
    "remove_works",
    "search_capability",
    "search_page",
    "sync",
]
