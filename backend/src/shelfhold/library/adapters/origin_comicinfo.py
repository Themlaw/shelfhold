"""The ComicInfo as the service sent it, kept in /data/meta (EF-BIB-048, GAP-9-13).

The exporter later replaces the ComicInfo of the file in /library with its own rendering, so once
a Unit becomes an irreplaceable candidate the original is no longer in any file Shelfhold holds.
A safeguard made from /library strips the rendering and keeps the pages (`store_provenance =
extracted`); the metadata the origin had given is what this file keeps. It is small (bounded to
1 MiB by the CBZ audit), lives in `/data` like `unit.json`, which refers to it, and is erased with
the Unit by a purge (`derived_rels`). Writing is best effort: a full disk costs the metadata, never
the chapter.
"""

import hashlib
from pathlib import Path
from typing import Final

from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.volumes import read_data_file, write_data_file

ORIGIN_DIR: Final = "meta/origin"
MAX_BYTES: Final = 1024 * 1024


def origin_rel(unit_id: str) -> str:
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return f"{ORIGIN_DIR}/{unit_id[:2]}/{unit_id}.xml"


def keep_origin_comicinfo(data_dir: Path, unit_id: str, xml: bytes) -> str:
    """Write the original ComicInfo of the Unit (atomic, owner only). Returns its path in /data."""
    if len(xml) > MAX_BYTES:
        raise ValueError("a ComicInfo is never larger than 1 MiB")
    rel = origin_rel(unit_id)
    write_data_file(data_dir, rel, xml)
    return rel


def origin_reference(data_dir: Path, unit_id: str) -> dict[str, str] | None:
    """What `unit.json` says of the kept ComicInfo: where it is and the hash of its bytes."""
    rel = origin_rel(unit_id)
    try:
        raw = read_data_file(data_dir / rel, MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if not raw or len(raw) > MAX_BYTES:
        return None
    return {"path": rel, "original_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
