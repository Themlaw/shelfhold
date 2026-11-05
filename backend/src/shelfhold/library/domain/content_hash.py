"""content_hash(): the hash of the original payload, whatever Shelfhold rendered around it.

One function, versioned (`CONTENT_HASH_BY`, kept in `content.content_hash_by`), the same for every
destination (EF-BIB-048, I25). For a comic archive the payload is its image pages, in the order of
the archive, each by name and by bytes. The files Shelfhold adds when it renders for a destination
(ComicInfo.xml) are not part of it: that is what lets the file in /library be recognised as the
one that was received, and a copy of it be counted by `independent_copies`. `file_hash` is the other
level: the hash of the rendered file as a whole, per destination.

Changing what is hashed changes the version: old rows keep the version that produced them.
EPUB (OPF rewritten by the export) is M2 and will bring `content-hash@2` next to this one.
"""

import hashlib
from typing import Final

from shelfhold.infrastructure.archives import SafeArchive

CONTENT_HASH_VERSION: Final = 1
CONTENT_HASH_BY: Final = f"content-hash@{CONTENT_HASH_VERSION}"
# The other level, named in a tombstone beside the payload hash: sha256 of a file as a whole
# (what `content.original_sha256` and `replica.file_hash` hold). An adoption or an import sees
# only the file, so a purged file is recognised by this one, whatever was rendered around it.
FILE_HASH_BY: Final = "sha256"

# Basenames (lower case) of what Shelfhold itself injects when it renders a comic archive.
RENDERED_SIDECARS: Final = frozenset({"comicinfo.xml"})


def is_rendered_sidecar(name: str) -> bool:
    return name.rsplit("/", 1)[-1].lower() in RENDERED_SIDECARS


def content_hash(archive: SafeArchive) -> str:
    """`sha256:<hex>` of the payload. The archive was audited by `safe_open_archive`."""
    digest = hashlib.sha256(f"{CONTENT_HASH_BY}\n".encode())
    for entry in archive.entries:
        if entry.is_dir or is_rendered_sidecar(entry.name):
            continue
        page = hashlib.sha256()
        with archive.open(entry.name) as stream:
            while chunk := stream.read(1 << 20):
                page.update(chunk)
        name = entry.name.encode("utf-8", "surrogatepass")
        digest.update(len(name).to_bytes(4, "big") + name + page.digest())
    return "sha256:" + digest.hexdigest()
