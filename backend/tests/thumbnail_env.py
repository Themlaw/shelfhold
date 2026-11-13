"""M3 L24: synthetic covers (Pillow, no network) and the rows that point an Edition at one."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from PIL import Image

from shelfhold.infrastructure.cover_store import CoverStore
from shelfhold.infrastructure.schema import catalog

FETCHED = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def webp_bytes(colour: tuple[int, int, int], size: tuple[int, int] = (900, 1200)) -> bytes:
    """A cover as the host stores it: a WebP, distinct for each colour (so, for each hash)."""
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "WEBP")
    return out.getvalue()


def put_cover(
    data_dir: Path, connection: sa.Connection, edition_id: str, colour: tuple[int, int, int]
) -> str:
    """Store a cover under `/data/covers` and point the Edition's `cover` field at it. Returns the
    hex digest that addresses it (the one of the thumbnail URL)."""
    ref = CoverStore(data_dir).put(webp_bytes(colour))
    point_at(connection, edition_id, ref)
    return ref.removeprefix("covers/").removesuffix(".webp")


def point_at(connection: sa.Connection, edition_id: str, ref: str) -> None:
    connection.execute(
        sa.delete(catalog.metadata_field).where(
            catalog.metadata_field.c.entity_id == edition_id,
            catalog.metadata_field.c.field == "cover",
        )
    )
    connection.execute(
        sa.insert(catalog.metadata_field).values(
            entity_type="edition", entity_id=edition_id, field="cover", lang="und",
            value=json.dumps(ref, separators=(",", ":")), provider="source-a",
            provenance="provider", fetched_at=FETCHED,
        )
    )  # fmt: skip


def thumbnail_files(data_dir: Path) -> list[Path]:
    folder = data_dir / "thumbnails"
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []
