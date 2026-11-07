"""The host keeps the file a `ranged_content` service delivered for a range (EF-SRC-032, 034).

M1a files the bytes and the coverage (`export_item` + `unit_coverage`, written by the ticket
cycle in the same transaction); the export of such a file is M2's (lncrawl is its first real
service). The bytes go to `<data>/ranged/<export_item id><ext>`, through the writer of `/data`
(atomic, owner-only), after a check against the digest of the download: a refusal raises, and the
record the caller holds open rolls back with it. The size is bounded by the receiver upstream.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa

from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.volumes import erase_data_path, write_data_file

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from shelfhold.library.adapters.receive import Received
    from shelfhold.sdk.refs import RangedPayload

__all__ = ["RangedKeeper"]

_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")
ORPHAN_AGE_S = 3600  # a file younger than this may belong to a transaction still open


class RangedKeeper:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)

    def __call__(self, received: Received, payload: RangedPayload, item_id: str) -> None:
        suffix = Path(payload.asset.filename).suffix.lower()
        if not _EXTENSION.fullmatch(suffix):
            suffix = ""
        data = received.path.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != received.sha256:
            raise ValueError("received_hash_mismatch: the kept copy is not the download")
        write_data_file(self._data_dir, f"ranged/{item_id}{suffix}", data)

    def sweep(self, engine: Engine, *, now: float | None = None) -> int:
        """Remove what a written-then-rolled-back transaction left in `<data>/ranged`: a file
        whose name is no `export_item` id and that is older than `ORPHAN_AGE_S`. Returns the
        number removed."""
        folder = self._data_dir / "ranged"
        if not folder.is_dir():
            return 0
        with engine.connect() as connection:
            known = set(connection.execute(sa.select(export.export_item.c.id)).scalars())
        clock = time.time() if now is None else now
        removed = 0
        for path in sorted(folder.iterdir()):
            if path.stem in known or clock - path.lstat().st_mtime < ORPHAN_AGE_S:
                continue
            removed += erase_data_path(self._data_dir, f"ranged/{path.name}")
        return removed
