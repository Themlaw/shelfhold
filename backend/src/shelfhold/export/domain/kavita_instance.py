"""The fingerprint of a Kavita instance (EF-EXP-039): what tells a reinstall from the same server.

The URL and the installation identifier that `server-info-slim` gives (Kavita draws a new one
when its database is created again). The ids and creation dates of the libraries would say the
same, but `LibraryDto` carries no date and a fresh install hands out the ids again from 1: they
cannot tell two installs apart, and a library added later would look like a reinstall.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit


def instance_fingerprint(base_url: str, install_id: str | None) -> str | None:
    """None when the server gives no identifier: nothing can then be compared."""
    if not install_id:
        return None
    parts = urlsplit(base_url.strip())
    origin = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port or ''}"
    digest = hashlib.sha256(
        f"{origin}\n{parts.path.rstrip('/')}\n{install_id}".encode()
    ).hexdigest()
    return "sha256:" + digest


def instance_changed(stored: str | None, current: str | None) -> bool:
    """A first sight is not a change; an unreadable identifier is not one either."""
    return stored is not None and current is not None and stored != current


def match_library(libraries: list[dict[str, object]], remote_root: str) -> dict[str, object] | None:
    """The library that already watches this folder: the path is the key, ids are a cache.

    A library whose folder is the root itself wins; failing that, the deepest folder that
    contains the root (one library over `/manga` serves a shelf rooted in `/manga/Kavita`).
    """
    root = remote_root.rstrip("/")
    best: tuple[int, dict[str, object]] | None = None
    for library in libraries:
        folders = library.get("folders")
        for folder in folders if isinstance(folders, list) else []:
            base = str(folder).rstrip("/")
            if base == root:
                return library
            if root.startswith(base + "/") and (best is None or len(base) > best[0]):
                best = (len(base), library)
    return best[1] if best else None
