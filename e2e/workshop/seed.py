"""Synthetic base of the workshop harness: three profiles (admin, member, a child of 8) and a
library of public domain works of every kind (novels, a manga, comic albums), with their authors,
a reading under way for each profile and requests waiting for the administrator. The one webtoon
is invented (none is in the public domain yet). Nothing reaches the network.

Run after `shelfhold migrate` and `shelfhold admin create`, before `shelfhold serve`:
    python seed.py <data-dir>
"""

import hashlib
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
from shelfhold.infrastructure.schema import catalog, export, identity, storage
from shelfhold.reading.application.progress import insert_position

PASSWORD = "synthetic-profile-password-1"  # noqa: S105 (invented, for the synthetic base)
COVERS = 60  # a library screen (ENF-PERF-010); the digests are written for `weight.spec.ts`
COVERS_FILE = "covers.json"
COVER_SEED = 20_261_113  # fixed: the same covers, so the same weight, on every run
TEMPLATE = json.dumps(
    {
        "chapter": "{series} - Chapter {number}", "volume": "{series} - Vol.{number}",
        "special": "{series} - {label}", "special_dir": "Specials",
        "segment": "{series} - Part {n}",
    }
)  # fmt: skip
# The works of the household collection: unrated, so the child reads none of them (and the
# household collection does not exist for her).
TITLES = (
    ("Michel Strogoff", "Jules Verne", "novel"),
    ("Les Voyages de Gulliver", "Jonathan Swift", "novel"),
    ("L'Île mystérieuse", "Jules Verne", "novel"),
)
# The volumes of the covered library (a stored cover each): (series, author, kind).
VOLUMES = (
    ("Hokusai Manga", "Katsushika Hokusai", "manga"),
    ("Little Nemo in Slumberland", "Winsor McCay", "comic"),
    ("Les Misérables", "Victor Hugo", "novel"),
    ("Le Comte de Monte-Cristo", "Alexandre Dumas", "novel"),
    ("Les Trois Mousquetaires", "Alexandre Dumas", "novel"),
    ("Contes", "Hans Christian Andersen", "novel"),
)
# The newest arrivals (the home shows them first), rated for every age unless said: no stored
# cover, so each gets the cover composed for its kind. (title, author, kind, age rating)
LIBRARY = (
    ("Le Phare des brumes", "Yun-seo Park", "webtoon", 10),
    ("Histoire de M. Vieux Bois", "Rodolphe Töpffer", "bd", 0),
    ("Max et Moritz", "Wilhelm Busch", "bd", 0),
    ("Contes de ma mère l'Oye", "Charles Perrault", "novel", 0),
    ("Fables", "Jean de La Fontaine", "novel", 0),
    ("Contes de l'enfance et du foyer", "Jacob et Wilhelm Grimm", "novel", 0),
    ("Alice au pays des merveilles", "Lewis Carroll", "novel", 0),
    ("Le Tour du monde en quatre-vingts jours", "Jules Verne", "novel", 0),
    ("Vingt mille lieues sous les mers", "Jules Verne", "novel", 0),
    ("Hokusai Manga, choix de planches", "Katsushika Hokusai", "manga", 0),
    ("Little Nemo, les rêves", "Winsor McCay", "comic", 0),
)
# What each profile is reading: the title of a work of LIBRARY, the chapter under way and how far.
READING = {"admin": ("Vingt mille lieues sous les mers", 2, 0.4),
           "membre": ("Hokusai Manga, choix de planches", 3, 0.6),
           "enfant8": ("Little Nemo, les rêves", 1, 0.3)}  # fmt: skip
# The chapters of the works that have some: public domain titles (Verne, 1870; La Fontaine, 1668),
# so that a series sheet and the reader have a list to show. The three works being read keep a copy
# of each chapter; the others (`SERIES`) only list theirs, as a source would.
CHAPTERS = {
    "Vingt mille lieues sous les mers": (
        "Un écueil fuyant", "Le pour et le contre", "Comme il plaira à monsieur",
        "Ned Land", "À l'aventure",
    ),
    "Hokusai Manga, choix de planches": ("Les gestes", "Les paysages", "Les esprits", "Les métiers", "Les foules"),
    "Little Nemo, les rêves": ("Le lit qui vole", "Le palais du Roi", "La chute", "Le réveil", "Le retour"),
}  # fmt: skip
SERIES = {
    "Fables": (
        "La Cigale et la Fourmi", "Le Corbeau et le Renard", "Le Loup et l'Agneau",
        "Le Lièvre et la Tortue", "La Laitière et le Pot au lait",
    ),
    "Le Phare des brumes": tuple(f"Épisode {n}" for n in range(1, 9)),
    "Alice au pays des merveilles": (
        "Dans le terrier du lapin", "La mare de larmes", "Une course au comité",
    ),
}  # fmt: skip
SEEDED = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
CONTENT_HASH = "sha256:" + "5e" * 32  # the copies of a reading under way (no file behind them)


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


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def stable_ulid(name: str) -> str:
    """A ULID drawn from a name: the same work keeps the same identifier, and so the same drawn
    cover, from one run of the harness to the next (a ULID starts with 0 to 7)."""
    digest = int.from_bytes(hashlib.sha256(name.encode()).digest()[:17], "big")
    chars = [CROCKFORD[(digest >> (5 * i)) & 31] for i in range(26)]
    return "0" + "".join(chars[1:])


def credit(db: sa.Connection, work_id: str, author: str) -> None:
    """The author of a Work, as a metadata provider names it (`contributors`)."""
    db.execute(
        sa.insert(catalog.metadata_field).values(
            entity_type="work", entity_id=work_id, field="contributors", lang="und",
            value=json.dumps([author], ensure_ascii=False), provider="source-a",
            provenance="provider", fetched_at=SEEDED,
        )
    )  # fmt: skip


def add_work(
    db: sa.Connection, shelf_id: str, title: str, author: str, kind: str, age: int | None = None
) -> tuple[str, str]:
    """A Work, its author and one Edition on the shelf; rated when `age` is given. Returns the
    ids of the Work and of its Edition."""
    work_id, edition_id = stable_ulid(f"work:{title}"), stable_ulid(f"edition:{title}")
    rated = SEEDED if age is not None else None
    db.execute(
        sa.insert(catalog.work).values(
            id=work_id, kind=kind, title=title, sort_title=title.casefold(), age_rating=age,
            rated_at=rated,
        )
    )  # fmt: skip
    db.execute(
        sa.insert(catalog.edition).values(
            id=edition_id, work_id=work_id, lang="fr", translation_type="official",
            media="text" if kind == "novel" else "images", shelf_id=shelf_id, age_rating=age,
            rated_at=rated,
        )
    )  # fmt: skip
    credit(db, work_id, author)
    return work_id, edition_id


def seed_covered_library(data_dir: Path, db: sa.Connection, shelf_id: str) -> None:
    """`COVERS` Works on the shelf, each with a stored cover (a real file under `/data/covers`,
    pointed at by its Edition's `cover` field); the digests go to `covers.json` beside the data."""
    rng = random.Random(COVER_SEED)  # noqa: S311 - a reproducible synthetic base
    store = CoverStore(data_dir)
    digests: list[str] = []
    for n in range(COVERS):
        series, author, kind = VOLUMES[n % len(VOLUMES)]
        _, edition_id = add_work(
            db, shelf_id, f"{series}, tome {n // len(VOLUMES) + 1}", author, kind
        )
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


def seed_destinations(db: sa.Connection) -> None:
    """The screens of the destinations (L33a): a Kavita (it keeps accounts apart by library), a
    plain shared folder (it keeps nothing apart: a family shelf is refused it), and an adult shelf
    that already has its Kavita binding, so that a member has an account to link. The address of
    the Kavita is never reached: the server does not call it unless a key is linked."""
    kavita, folder, adult = new_ulid(), new_ulid(), new_ulid()
    db.execute(
        sa.insert(export.export_target),
        [
            {"id": kavita, "kind": "kavita", "name": "Kavita maison",
             "config": {"url": "http://kavita.invalid"}},
            {"id": folder, "kind": "native_folder", "name": "Dossier partagé", "config": {}},
        ],
    )  # fmt: skip
    db.execute(sa.insert(export.shelf).values(id=adult, name="Adultes", phrase="Pour les grands"))
    db.execute(
        sa.insert(export.shelf_destination).values(
            id=new_ulid(), shelf_id=adult, target_id=kavita, transport="tree", egress="foyer",
            root_subpath="adultes", template=TEMPLATE, template_origin="default@1",
        )
    )  # fmt: skip


def seed_collections(db: sa.Connection, member_id: str, work_ids: list[str]) -> None:
    """The screens of the collections (L33b): a collection of the household (the three Works, in
    order) and a private list of the member (one Work with its number). All Works of this base are
    unrated, so the child reads neither (and the household collection does not exist for them)."""

    def make(name: str, kind: str, owner: str | None, works: list[str]) -> None:
        collection_id = new_ulid()
        db.execute(
            sa.insert(catalog.collection).values(
                id=collection_id, kind=kind, name=name, owner_id=owner
            )
        )
        for position, work_id in enumerate(works, start=1):
            db.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=collection_id, position=position,
                    work_id=work_id, number=str(position),
                )
            )  # fmt: skip

    make("Lectures de l'été", "collection", None, work_ids)
    make("À lire ensuite", "reading_list", member_id, work_ids[:1])


def seed_reading(
    db: sa.Connection,
    user_id: str,
    edition_id: str,
    chapter: int,
    percent: float,
    titles: tuple[str, ...] = (),
) -> None:
    """A reading under way: five chapters, each with a kept copy (adopted in place: the home only
    needs to know there is one), and a position in `chapter`, as the reader would have sent it."""
    text = (
        db.execute(
            sa.select(catalog.edition.c.media).where(catalog.edition.c.id == edition_id)
        ).scalar_one()
        == "text"
    )
    under_way = ""
    for number in range(1, 6):
        unit_id, content_id = new_ulid(), new_ulid()
        db.execute(
            sa.insert(catalog.unit).values(
                id=unit_id, edition_id=edition_id, number_raw=str(number), number_norm=str(number),
                number_sort=float(number), track="main", kind="chapter",
                title=titles[number - 1] if number <= len(titles) else None,
            )
        )  # fmt: skip
        db.execute(
            sa.insert(storage.content).values(
                id=content_id, unit_id=unit_id, variant_id=None,
                kind="text" if text else "images",
                media_type="application/epub+zip" if text else "application/vnd.comicbook+zip",
                format="epub" if text else "cbz", retention="adopted_ref", path=None,
                original_sha256="sha256:" + f"{number:x}" * 64, store_provenance="received",
                size=1, content_hash=CONTENT_HASH, content_hash_by="norm@1",
            )
        )  # fmt: skip
        db.execute(
            sa.insert(export.unit_coverage).values(
                carrier_type="content", carrier_id=content_id, unit_id=unit_id, position=1,
                origin="manual",
            )
        )  # fmt: skip
        if number == chapter:
            under_way = content_id
    insert_position(
        db, user_id=user_id, carrier_type="content", carrier_id=under_way,
        content_hash=CONTENT_HASH, locator={"v": 1, "type": "page", "index": 12, "count": 30},
        percent=percent, client_ts=SEEDED, device_id="liseuse", source="device", now=SEEDED,
        traverse=False,
    )  # fmt: skip


def seed_chapters(db: sa.Connection, edition_id: str, titles: tuple[str, ...]) -> None:
    """The chapters a source lists for an Edition, with no file yet: the series sheet shows them as
    available to fetch."""
    for number, title in enumerate(titles, start=1):
        db.execute(
            sa.insert(catalog.unit).values(
                id=new_ulid(), edition_id=edition_id, number_raw=str(number),
                number_norm=str(number), number_sort=float(number), track="main", kind="chapter",
                title=title,
            )
        )  # fmt: skip


def seed_requests(db: sa.Connection, users: dict[str, str]) -> None:
    """What waits for the administrator: a request of the child (it needs an age) and one of the
    member; both are free text, so nothing is searched in a source."""
    for name, wanted in (
        ("enfant8", "Les Contes du chat perché"),
        ("membre", "Le Grand Meaulnes"),
    ):
        db.execute(
            sa.insert(identity.request).values(
                id=new_ulid(), requester_user_id=users[name], target_kind="free_text",
                target_value=wanted, state="pending", shown_title=wanted,
            )
        )  # fmt: skip


def main(data_dir: Path) -> None:
    engine = create_db_engine(data_dir / "shelfhold.db")
    digest = Argon2Hasher().hash(PASSWORD)
    with engine.begin() as db:
        # A child always has an age ceiling (migration 0046): 7 for the child of 8.
        member_id = new_ulid()
        for name, role, ceiling in (("membre", "member", None), ("enfant8", "child", 7)):
            db.execute(
                sa.insert(identity.user).values(
                    id=member_id if role == "member" else new_ulid(),
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
        seed_destinations(db)
        work_ids = [add_work(db, shelf_id, *work)[0] for work in TITLES]
        seed_covered_library(data_dir, db, shelf_id)
        editions = {
            title: add_work(db, shelf_id, title, author, kind, age)[1]
            for title, author, kind, age in LIBRARY
        }
        seed_collections(db, member_id, work_ids)
        users = {
            str(r.username): str(r.id)
            for r in db.execute(sa.select(identity.user.c.id, identity.user.c.username))
        }
        for name, (title, chapter, percent) in READING.items():
            seed_reading(
                db, users[name], editions[title], chapter, percent, CHAPTERS.get(title, ())
            )
        for title, titles in SERIES.items():
            seed_chapters(db, editions[title], titles)
        seed_requests(db, users)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
