"""The FTS tables, seen from the search package: the one import the contract `fts-sole-reader`
sanctions (the contract needs a real import to match its ignore until M3 L17 adds the queries)."""

from shelfhold.infrastructure.schema import search as fts_tables

__all__ = ["fts_tables"]
