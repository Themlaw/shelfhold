"""A synthetic /library, /store and /data for the library tests: real files, a real migrated DB.

`library` and `store` are two physical roots with their own sentinel, as in production; `data`
holds `meta/`. Nothing is mocked: the file system and SQLite are the real ones.
"""

import io
import shutil
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import Engine
from ulid import ULID

from shelfhold.export.domain.path_map import PathEntry, PathMap
from shelfhold.export.domain.physical_roots import ExpectedRoot, SentinelDocument
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops, create_sentinel
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from tests.catalog_env import database
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 24
OLD_COMICINFO = b"<ComicInfo><Series>Upstream</Series></ComicInfo>"
RENDERED_COMICINFO = b"<ComicInfo><Series>Kavita</Series><Number>1</Number></ComicInfo>"


def cbz_bytes(
    pages: tuple[tuple[str, bytes], ...] = (("001.png", PNG), ("002.jpg", JPEG)),
    comicinfo: bytes | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in pages:
            zf.writestr(name, data)
        if comicinfo is not None:
            zf.writestr("ComicInfo.xml", comicinfo)
    return buffer.getvalue()


@dataclass
class LibEnv:
    tmp: Path
    engine: Engine
    library: Path
    manga: Path
    store: Path
    data: Path
    fsops: Fsops
    library_root: str
    store_root: str
    chain: dict[str, str]

    def locate(self, local: str) -> tuple[str, str]:
        """(root_id, path relative to the root) of a local path; the composition root's job."""
        for root_id, base in ((self.store_root, self.store), (self.library_root, self.library)):
            try:
                return root_id, Path(local).relative_to(base).as_posix()
            except ValueError:
                continue
        raise ValueError(local)

    def stage(self, data: bytes, name: str = "received.cbz") -> Path:
        path = self.tmp / "staging" / f"{new_ulid()}-{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def new_unit(self) -> tuple[str, str]:
        """Another Unit and Variant in the same Edition and binding."""
        with self.engine.begin() as connection:
            number = str(
                int(connection.execute(sa.text("SELECT count(*) FROM unit")).scalar_one()) + 1
            )
            unit_id = add_unit(connection, self.chain["edition"], number)
            variant_id = add_variant(connection, unit_id, self.chain["binding"], f"ch-{number}")
        return unit_id, variant_id

    def files_under(self, root: Path) -> list[Path]:
        return sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and ".shelfhold" not in p.parts and p.name != ".shelfhold-mount-id"
        )


@contextmanager
def lib_env() -> Iterator[LibEnv]:
    with database() as db_path, tempfile.TemporaryDirectory(prefix="shelfhold-lib-") as raw:
        tmp = Path(raw)
        library, store, data = tmp / "library", tmp / "store", tmp / "data"
        manga = library / "manga"
        for directory in (manga, store, data):
            directory.mkdir(parents=True)
        instance_id = str(ULID())
        library_root, store_root = str(ULID()), str(ULID())
        expected = [
            ExpectedRoot(library_root, str(library), instance_id, 1),
            ExpectedRoot(store_root, str(store), instance_id, 1),
        ]
        for root_id, path in ((library_root, library), (store_root, store)):
            create_sentinel(
                path, SentinelDocument(root_id=root_id, instance_id=instance_id, generation=1)
            )
        path_map = PathMap((PathEntry(str(manga), "/manga"), PathEntry(str(store), "/store")))
        engine = create_db_engine(db_path)
        with engine.begin() as connection:
            for root_id, path in ((library_root, library), (store_root, store)):
                connection.execute(
                    sa.insert(platform.physical_root).values(
                        root_id=root_id, path_entry=str(path), generation=1
                    )
                )
            work_id = add_work(connection)
            edition_id = add_edition(connection, work_id)
            binding_id = add_binding(connection, work_id, edition_id, add_source_item(connection))
            unit_id = add_unit(connection, edition_id)
            variant_id = add_variant(connection, unit_id, binding_id)
        chain = {
            "work": work_id,
            "edition": edition_id,
            "binding": binding_id,
            "unit": unit_id,
            "variant": variant_id,
        }
        try:
            yield LibEnv(
                tmp, engine, library, manga, store, data,
                Fsops.open(path_map, expected), library_root, store_root, chain,
            )  # fmt: skip
        finally:
            engine.dispose()
            shutil.rmtree(tmp / "staging", ignore_errors=True)
