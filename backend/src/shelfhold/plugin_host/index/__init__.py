"""The signed plugin index and its client (EF-SRC-017, D14, ADR-0056).

`minisign` verifies the signature (prehashed `ED`, trusted comment, pinned key id);
`document` parses the v1 document strictly; `service` pins repositories, refreshes them
through the host's Fetcher, keeps revocations, rotates the pin on a double signature and
fetches wheels whose sha256 the index vouches for. No repository is preconfigured, and no
secret key lives anywhere in the product (D-P3).
"""

from shelfhold.plugin_host.index.document import IndexDocument, IndexEntry, parse_document
from shelfhold.plugin_host.index.minisign import (
    PinnedKey,
    PluginIndexError,
    parse_public_key,
    verify_detached,
)
from shelfhold.plugin_host.index.service import (
    RefreshOutcome,
    RepoRecord,
    RepoStore,
    index_fetcher,
)

__all__ = [
    "IndexDocument",
    "IndexEntry",
    "PinnedKey",
    "PluginIndexError",
    "RefreshOutcome",
    "RepoRecord",
    "RepoStore",
    "index_fetcher",
    "parse_document",
    "parse_public_key",
    "verify_detached",
]
