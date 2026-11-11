"""Build the exported EPUB: the OPF rewritten, every other entry copied as it was (ADR-0033).

Entries keep their order (`mimetype` stays first and stored), their names and their bytes, which
covers `META-INF/` and any watermark a purchased book carries. The compression of an entry follows
the source (an entry that was stored stays stored). The archive is written by `ArchiveWriter`
(fixed timestamps, nothing about the host or the tool) and read by `safe_open_archive`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO

from shelfhold.infrastructure.archives import ArchiveWriter, SafeArchive
from shelfhold.infrastructure.packaging.epub.opf import OpfEdit, find_opf_path, rewrite_opf


@dataclass(frozen=True, slots=True)
class RepackagedEpub:
    opf_path: str
    original_opf: bytes  # to keep in /data/meta; never in the file or its mirror (EF-BIB-048)
    changed: bool


def repackage_epub(source: SafeArchive, edit: OpfEdit, out: BinaryIO) -> RepackagedEpub:
    """Write `source` to `out` with its OPF edited. Nothing is written if the OPF is refused."""
    opf_path = find_opf_path(source)
    original = bytes(source.read(opf_path))
    rewritten = rewrite_opf(original, edit)
    with ArchiveWriter(out) as writer:
        for entry in source.entries:
            if entry.name == opf_path:
                writer.add(entry.name, rewritten.opf, compress=True)
            elif entry.is_dir:
                writer.add(entry.name, b"")
            else:
                packed = entry.name != "mimetype" and entry.compressed_size < entry.size
                with source.open(entry.name) as data:
                    writer.add(entry.name, data, compress=packed)
    return RepackagedEpub(opf_path, original, rewritten.changed)
