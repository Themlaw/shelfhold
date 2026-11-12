"""`store/INDEX.jsonl`: what /store says about itself if /data is lost (EF-INT-027 (2)).

One JSON object per kept file, in the order of the Unit ULID: `unit`, `path` (under /store),
`title` (the Work's), `lang`, `number` and `sha256` of the stored file. It is a projection of the
database, rewritten in the maintenance window (ENF-OPS-014) and only when it would change, so a
quiet night wakes no disk; deleting it loses nothing, the next pass writes it again byte for byte.

What it never holds: a line for a content that matches a tombstone (EF-BIB-052, EF-BIB-053), a
secret, a path outside /store, a destination's name. The write is atomic and goes through `fsops`
(a reader sees the old file or the new one, never half of one)."""

import json
from collections.abc import Callable, Collection
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.library.adapters.storage import ContentTombstones
from shelfhold.library.adapters.trash import units_binned_from_store
from shelfhold.library.domain.content_hash import CONTENT_HASH_BY, FILE_HASH_BY
from shelfhold.library.replica import kept_file_hashes
from shelfhold.library.tombstones.values import ContentHash

INDEX_NAME: Final = "INDEX.jsonl"


@dataclass(frozen=True)
class IndexReport:
    lines: int
    tombstoned: int  # kept contents left out because a tombstone names them
    rewritten: bool  # False: the file already said exactly this


class StoreIndex:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        store_dir: str,
        tombstones: ContentTombstones,
        *,
        hold: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._hold = hold  # the lock of the index, shared with `purge --now`
        self._engine = engine
        self._fsops = fsops
        self._path = f"{store_dir.rstrip('/')}/{INDEX_NAME}"
        self._tombstones = tombstones

    @property
    def path(self) -> str:
        return self._path

    def render(self, *, without_units: Collection[str] = ()) -> tuple[bytes, int, int]:
        """The file as it would be written, its line count and how many contents were left out.
        `without_units`: Units a purge is erasing, which the file must not cite (EF-BIB-052)."""
        content, unit, edition, work = storage.content, catalog.unit, catalog.edition, catalog.work
        with self._engine.connect() as connection:
            hashes = kept_file_hashes(connection)
            binned = units_binned_from_store(connection)  # in a bin for 30 days (EF-BIB-021)
            rows = connection.execute(
                sa.select(
                    content.c.unit_id,
                    content.c.path,
                    content.c.content_hash,
                    content.c.original_sha256,
                    work.c.title,
                    edition.c.lang,
                    unit.c.number_raw,
                )
                .select_from(
                    content.join(unit, unit.c.id == content.c.unit_id)
                    .join(edition, edition.c.id == unit.c.edition_id)
                    .join(work, work.c.id == edition.c.work_id)
                )
                .where(content.c.retention == "keep", content.c.path.is_not(None))
                .order_by(content.c.unit_id, content.c.id)
            ).all()
        lines: list[str] = []
        left_out = 0
        excluded = set(without_units) | binned
        for row in rows:
            if row.unit_id in excluded:
                left_out += 1
                continue
            stored = hashes.get(row.unit_id, row.original_sha256)
            wanted = [ContentHash(FILE_HASH_BY, stored)]
            if row.content_hash is not None:
                wanted.append(ContentHash(CONTENT_HASH_BY, row.content_hash))
            if self._tombstones.check_content(wanted):
                left_out += 1
                continue
            lines.append(
                json.dumps(
                    {
                        "unit": row.unit_id,
                        "path": row.path,
                        "title": row.title,
                        "lang": row.lang,
                        "number": row.number_raw,
                        "sha256": stored,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        text = "".join(f"{line}\n" for line in lines)
        return text.encode("utf-8"), len(lines), left_out

    def rewrite(self) -> IndexReport:
        """Rendered and written under the lock of the index: a purge that rewrites the file in
        between (its Units gone from the index before their copies go) is never overwritten by a
        render that was made before it (EF-BIB-052)."""
        with self._hold():
            data, count, left_out = self.render()
            try:
                same = self._fsops.read_bytes(self._path) == data
            except (FileNotFoundError, FsopsError):
                same = False
            if not same:
                self._fsops.write_atomic(self._path, data, replace=True)
        return IndexReport(count, left_out, rewritten=not same)
