"""Synthetic base of the workshop harness: three profiles (admin, member, a child of 8) and a
few Works that wait in "À classer". Everything is invented here; nothing reaches the network.

Run after `shelfhold migrate` and `shelfhold admin create`, before `shelfhold serve`:
    python seed.py <data-dir>
"""

import io
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from PIL import Image, ImageDraw

from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.infrastructure.cover_store import CoverStore
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity

PASSWORD = "synthetic-profile-password-1"  # noqa: S105 (invented, for the synthetic base)
COVERS = 60  # a library screen (ENF-PERF-010); the digests are written for `weight.spec.ts`
COVERS_FILE = "covers.json"
COVER_SEED = 20_261_113  # fixed: the same covers, so the same weight, on every run
TITLES = ("Le Jardin des brumes", "Chroniques du phare", "Atlas des marées")


def synthetic_cover(rng: random.Random) -> bytes:
    """A 900x1200 cover that weighs what a real one does: a gradient, flat panels and fine
    lines (drawn from the seeded generator), then a little grain; WebP, as the host stores it."""
    width, height = 900, 1200
    image = Image.new("RGB", (width, height))
    top, bottom = (rng.randrange(256) for _ in range(3)), (rng.randrange(256) for _ in range(3))
    top, bottom = tuple(top), tuple(bottom)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        mix = y / height
        draw.line(
            [(0, y), (width, y)], fill=tuple(int(a + (b - a) * mix) for a, b in zip(top, bottom))
        )
    for _ in range(40):
        x, y = rng.randrange(width), rng.randrange(height)
        box = (x, y, x + rng.randrange(40, 300), y + rng.randrange(40, 300))
        draw.rectangle(box, fill=tuple(rng.randrange(256) for _ in range(3)))
        draw.line([box[:2], box[2:]], fill=(20, 20, 20), width=rng.randrange(1, 4))
    grain = Image.frombytes("L", (width, height), rng.randbytes(width * height))
    image = Image.blend(image, Image.merge("RGB", (grain, grain, grain)), 0.06)
    out = io.BytesIO()
    image.save(out, "WEBP", quality=80)
    return out.getvalue()


def seed_covered_library(data_dir: Path, db: sa.Connection, shelf_id: str) -> None:
    """`COVERS` Works on the shelf, each with a stored cover (a real file under `/data/covers`,
    pointed at by its Edition's `cover` field); the digests go to `covers.json` beside the data."""
    rng = random.Random(COVER_SEED)  # noqa: S311 - a reproducible synthetic base
    store = CoverStore(data_dir)
    digests: list[str] = []
    for n in range(COVERS):
        title = f"Couverture {n:02d}"
        work_id, edition_id = new_ulid(), new_ulid()
        db.execute(
            sa.insert(catalog.work).values(
                id=work_id, kind="manga", title=title, sort_title=title.casefold()
            )
        )
        db.execute(
            sa.insert(catalog.edition).values(
                id=edition_id, work_id=work_id, lang="fr", translation_type="official",
                media="images", shelf_id=shelf_id,
            )
        )  # fmt: skip
        ref = store.put(synthetic_cover(rng))
        db.execute(
            sa.insert(catalog.metadata_field).values(
                entity_type="edition", entity_id=edition_id, field="cover", lang="und",
                value=json.dumps(ref, separators=(",", ":")), provider="source-a",
                provenance="provider", fetched_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
            )
        )  # fmt: skip
        digests.append(ref.removeprefix("covers/").removesuffix(".webp"))
    (data_dir.parent / COVERS_FILE).write_text(json.dumps(digests), encoding="utf-8")


def main(data_dir: Path) -> None:
    engine = create_db_engine(data_dir / "shelfhold.db")
    digest = Argon2Hasher().hash(PASSWORD)
    with engine.begin() as db:
        # A child always has an age ceiling (migration 0046): 7 for the child of 8.
        for name, role, ceiling in (("membre", "member", None), ("enfant8", "child", 7)):
            db.execute(
                sa.insert(identity.user).values(
                    id=new_ulid(),
                    username=name,
                    password_hash=digest,
                    role=role,
                    can_rate=False,
                    max_age_rating=ceiling,
                )
            )
        shelf_id = new_ulid()
        db.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Famille", phrase="Pour toute la maison", max_age_rating=13
            )
        )
        for title in TITLES:
            work_id = new_ulid()
            db.execute(
                sa.insert(catalog.work).values(
                    id=work_id, kind="manga", title=title, sort_title=title.casefold()
                )
            )
            db.execute(
                sa.insert(catalog.edition).values(
                    id=new_ulid(),
                    work_id=work_id,
                    lang="fr",
                    translation_type="official",
                    media="images",
                    shelf_id=shelf_id,
                )
            )
        seed_covered_library(data_dir, db, shelf_id)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
