"""Which Units the tombstones cover (EF-BIB-053), read from the database alone.

A database can hold a Unit that was purged after it was written: a backup put back (`restore`), or
a purge that stopped before its rows went. This is the one question both ask, whether a Unit is
covered by a `variant`, `place` or `series_binding` tombstone, so it is answered here once. A Unit
is covered as soon as one of its Variants is: the cautious side, nothing of it is let through.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import acquisition, catalog, storage

__all__ = ["UnitScreen", "blocked_units"]


class UnitScreen(Protocol):
    """The three questions of the tombstone screen that a Unit's rows can answer."""

    def variant_blocked(
        self, root_id: str | None, plugin_id: str, source_key: str, remote_unit_id: str,
        content_hashes: Sequence[tuple[str, str]],
    ) -> bool: ...  # fmt: skip

    def place_blocked(
        self, root_id: str | None, edition_key: Mapping[str, str], track: str, number_segment: str,
        number_raw: str, part: str,
    ) -> bool: ...  # fmt: skip

    def series_blocked(
        self, root_id: str | None, plugin_id: str, source_key: str, series_ref: str
    ) -> bool: ...


def blocked_units(connection: Connection, screen: UnitScreen) -> dict[str, str]:
    """`unit id -> scope` (`variant`, `place` or `series_binding`) of every Unit a tombstone covers.

    The layout is the one the purge records: a Variant is `(plugin id, source key, remote unit id)`,
    a place is the Edition's key and the Unit's place, a series is `(plugin id, source key, series
    ref)`."""
    unit, variant, edition = catalog.unit, catalog.unit_variant, catalog.edition
    binding, item, stored = acquisition.source_binding, acquisition.source_item, storage.content
    rows = connection.execute(
        sa.select(
            unit.c.id, unit.c.track, unit.c.number_segment, unit.c.number_raw, unit.c.part,
            edition.c.work_id, edition.c.lang, edition.c.translation_type, edition.c.media,
            edition.c.publisher, edition.c.text_version, edition.c.performance,
            edition.c.packaging, variant.c.remote_unit_id, item.c.plugin_id, item.c.source_key,
            item.c.remote_id, stored.c.content_hash, stored.c.content_hash_by,
        )
        .select_from(
            unit.join(edition, edition.c.id == unit.c.edition_id)
            .outerjoin(variant, variant.c.unit_id == unit.c.id)
            .outerjoin(binding, binding.c.id == variant.c.source_binding_id)
            .outerjoin(item, item.c.id == binding.c.source_item_id)
            .outerjoin(stored, stored.c.variant_id == variant.c.id)
        )
    )  # fmt: skip
    scopes: dict[str, str] = {}
    for row in rows:
        if row.id in scopes:
            continue
        hashes = (
            [(row.content_hash_by, row.content_hash)]
            if row.content_hash and row.content_hash_by
            else []
        )
        if (
            row.remote_unit_id
            and row.source_key
            and screen.variant_blocked(
                None, row.plugin_id, row.source_key, row.remote_unit_id, hashes
            )
        ):
            scopes[row.id] = "variant"
        elif screen.place_blocked(
            None,
            {
                "work_id": row.work_id, "lang": row.lang,
                "translation_type": row.translation_type, "media": row.media,
                "publisher": row.publisher, "text_version": row.text_version,
                "performance": row.performance, "packaging": row.packaging,
            },
            row.track, row.number_segment, row.number_raw, row.part,
        ):  # fmt: skip
            scopes[row.id] = "place"
        elif (
            row.source_key
            and row.remote_id
            and screen.series_blocked(None, row.plugin_id, row.source_key, row.remote_id)
        ):
            scopes[row.id] = "series_binding"
    return scopes
