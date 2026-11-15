"""M4 L49a: exported chapters on a real disk, for the block of a series.

Everything is synthetic: a chapter is a small CBZ written with `zipfile`, its pages are a few
bytes with the magic number of their format. The database holds what the exporter would have
left: a destination whose path map points at a temporary folder, one `export_item` per chapter
with its Unit and coverage, and one live `export_record` with the path, size and hash of the file.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export

PNG = b"\x89PNG\r\n\x1a\n"
JPG = b"\xff\xd8\xff\xe0"
WEBP = b"RIFF\x00\x00\x00\x00WEBP"
GIF = b"GIF89a"
AVIF = b"\x00\x00\x00\x1cftypavif"
MAGIC = {"png": PNG, "jpg": JPG, "webp": WEBP, "gif": GIF, "avif": AVIF}


def page(ext: str, tag: str, size: int = 200) -> bytes:
    """Bytes of a page: the magic number of the format, then a tag that is the page itself."""
    body = (tag.encode() + b"|") * (size // (len(tag) + 1) + 1)
    return MAGIC[ext] + body[:size]


@dataclass
class Chapter:
    number: str
    pages: list[tuple[str, bytes]]  # (name inside the CBZ, bytes), in archive order
    fmt: str = "cbz"  # `epub` and `pdf`: a file the block does not take
    deflate: bool = False
    extra: list[tuple[str, bytes]] = field(default_factory=list)  # entries after the pages

    @classmethod
    def simple(cls, number: str, count: int = 3, ext: str = "png", **kw: object) -> Chapter:
        pages = [(f"{n:05d}.{ext}", page(ext, f"ch{number}p{n}")) for n in range(1, count + 1)]
        return cls(
            number, pages,
            extra=[("ComicInfo.xml", b"<ComicInfo><Title>x</Title></ComicInfo>")], **kw,  # type: ignore[arg-type]
        )  # fmt: skip


@dataclass
class Exported:
    item_id: str
    unit_id: str
    path: Path
    rel_path: str


@dataclass
class Library:
    engine: Engine
    root: Path
    destination_id: str
    edition_id: str
    items: list[Exported] = field(default_factory=list)
    _order: int = 0

    def add(self, chapter: Chapter, *, rel_path: str | None = None) -> Exported:
        rel = rel_path or f"Series/ch-{chapter.number}.{chapter.fmt}"
        path = self.root / rel
        write_cbz(path, chapter)
        return self._record(chapter, rel, path)

    def add_raw(self, chapter: Chapter, rel_path: str, *, at: Path | None = None) -> Exported:
        """A record whose `rel_path` is as given (a climbing path, an absolute one), whatever
        file is at `at` (outside the root, for a traversal)."""
        if at is not None:
            write_cbz(at, chapter)
        return self._record(chapter, rel_path, at or self.root / "missing.cbz")

    def _record(self, chapter: Chapter, rel: str, path: Path) -> Exported:
        self._order += 1
        item_id, unit_id = new_ulid(), new_ulid()
        data = path.read_bytes() if path.is_file() else b""
        stat = path.stat() if path.is_file() else None
        with write_transaction(self.engine) as c:
            c.execute(
                sa.insert(catalog.unit).values(
                    id=unit_id, edition_id=self.edition_id, number_raw=chapter.number,
                    number_norm=chapter.number, number_sort=float(chapter.number),
                    source_order=self._order,
                )
            )  # fmt: skip
            c.execute(
                sa.insert(export.export_item).values(
                    id=item_id, edition_id=self.edition_id, kind="unit"
                )
            )
            c.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=item_id, unit_id=unit_id,
                    position=1, origin="manual",
                )
            )  # fmt: skip
            c.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=self.destination_id, export_item_id=item_id,
                    locator_type="path", rel_path=rel, format=chapter.fmt, state="resolved",
                    size=len(data), mtime_ns=stat.st_mtime_ns if stat else None,
                    file_hash="sha256:" + hashlib.sha256(data).hexdigest(),
                )
            )  # fmt: skip
        found = Exported(item_id, unit_id, path, rel)
        self.items.append(found)
        return found

    def rewrite(self, exported: Exported, chapter: Chapter, *, record: bool) -> None:
        """The file replaced by a new one (a rename, so a descriptor already open keeps the old
        inode). With `record`, the manifest follows: the exporter's own rewrite."""
        temp = exported.path.with_suffix(".tmp")
        write_cbz(temp, chapter)
        os.replace(temp, exported.path)
        if record:
            data = exported.path.read_bytes()
            with write_transaction(self.engine) as c:
                c.execute(
                    sa.update(export.export_record)
                    .where(export.export_record.c.export_item_id == exported.item_id)
                    .values(
                        size=len(data), mtime_ns=exported.path.stat().st_mtime_ns,
                        file_hash="sha256:" + hashlib.sha256(data).hexdigest(),
                    )
                )  # fmt: skip

    def overwrite_in_place(self, exported: Exported, chapter: Chapter) -> None:
        """The same inode, new bytes: what a descriptor kept open cannot protect against."""
        buffer = exported.path.with_suffix(".tmp")
        write_cbz(buffer, chapter)
        with exported.path.open("r+b") as target:
            target.write(buffer.read_bytes())
        buffer.unlink()


def write_cbz(path: Path, chapter: Chapter) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kind = zipfile.ZIP_DEFLATED if chapter.deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", kind) as archive:
        for name, data in [*chapter.pages, *chapter.extra]:
            archive.writestr(name, data)


def library_for(engine: Engine, root: Path, edition_id: str) -> Library:
    """A destination on `root` (a tree) for the shelf of the Edition's Work, and nothing exported."""
    root.mkdir(parents=True, exist_ok=True)
    target_id, shelf_id, destination_id = new_ulid(), new_ulid(), new_ulid()
    with write_transaction(engine) as c:
        c.execute(
            sa.insert(export.export_target).values(
                id=target_id, kind="kavita", name=f"t-{target_id}",
                path_map=[{"local": str(root), "remote": "/m", "mode": "rw"}],
            )
        )  # fmt: skip
        c.execute(
            sa.insert(export.shelf).values(id=shelf_id, name=f"s-{shelf_id}", phrase="manga")
        )
        c.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id, shelf_id=shelf_id, target_id=target_id, transport="tree",
                egress="foyer", root_subpath="", template="{series}", template_origin="default@1",
            )
        )  # fmt: skip
    return Library(engine, root, destination_id, edition_id)
