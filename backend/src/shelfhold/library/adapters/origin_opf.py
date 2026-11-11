"""The OPF of an EPUB as it was before the exporter edited it, kept in /data/meta (EF-BIB-048).

Same rules as the original ComicInfo (`origin_comicinfo`): small, in `/data` beside `unit.json`,
which refers to it, erased with the Unit by a purge, and never inside the exported file nor its
provenance mirror. The first one kept is the original: a later call does not replace it, so a
re-render from an already edited file cannot overwrite it with an edited OPF. Writing is best
effort for the caller: a full disk costs the metadata, never the book.
"""

import hashlib
from pathlib import Path
from typing import Final

from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.volumes import read_data_file, write_data_file

ORIGIN_OPF_DIR: Final = "meta/origin-opf"
MAX_BYTES: Final = 8 * 1024 * 1024  # the cap of `safe_parse_xml`: a larger OPF is never read


def origin_opf_rel(unit_id: str) -> str:
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return f"{ORIGIN_OPF_DIR}/{unit_id[:2]}/{unit_id}.opf"


def keep_origin_opf(data_dir: Path, unit_id: str, opf: bytes) -> str:
    """Keep the original OPF of the Unit unless one is already kept. Returns its path in /data."""
    if len(opf) > MAX_BYTES:
        raise ValueError("an OPF is never larger than 8 MiB")
    rel = origin_opf_rel(unit_id)
    if not (data_dir / rel).exists():
        write_data_file(data_dir, rel, opf)
    return rel


def origin_opf_reference(data_dir: Path, unit_id: str) -> dict[str, str] | None:
    """What `unit.json` says of the kept OPF: where it is and the hash of its bytes."""
    rel = origin_opf_rel(unit_id)
    try:
        raw = read_data_file(data_dir / rel, MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    if not raw or len(raw) > MAX_BYTES:
        return None
    return {"path": rel, "original_sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
