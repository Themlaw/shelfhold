"""The fingerprint of a Kavita instance (EF-EXP-039): what tells a reinstall from the same server.

Three things: the URL, the `installId` of `server-info-slim`, and the creation date of the oldest
account (`GET /api/Users`). `installId` is **not** random: Kavita derives it with a CRC32 of
stable data (`Seed.cs`, `HashUtil.cs`), so a database wiped on a machine that kept `appsettings`
gives the same one back. The date of the first account is new with every database. The ids and
creation dates of the libraries would not do: `LibraryDto` has no date and a fresh install hands
out the ids again from 1.

Residual risk, documented in the admin pages: a restored backup of the database and a
reinstall that keeps both `appsettings` and the first account look like the same instance. The
access audit therefore never trusts a cached library id: it follows the folders (EF-EXP-055).
"""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


def instance_fingerprint(
    base_url: str, install_id: str | None, first_account_created: str | None = None
) -> str | None:
    """None when the server gives no installation id: nothing can then be compared."""
    if not install_id:
        return None
    parts = urlsplit(base_url.strip())
    origin = f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{parts.port or ''}"
    digest = hashlib.sha256(
        f"{origin}\n{parts.path.rstrip('/')}\n{install_id}\n{first_account_created or ''}".encode()
    ).hexdigest()
    return "sha256:" + digest


def instance_changed(stored: str | None, current: str | None) -> bool:
    """A first sight is not a change; an unreadable identifier, once one was known, is one."""
    return stored is not None and stored != current


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


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/").strip() or "/").casefold()


def covering_libraries(libraries: Sequence[Mapping[str, Any]], remote_root: str) -> list[int]:
    """Ids of every library with a folder that contains the root or lies inside it.

    Whoever reaches such a library reaches the files of the shelf. Paths are compared once
    normalised, never by the id a link cached.
    """
    root = _norm(remote_root)
    found: list[int] = []
    for library in libraries:
        folders = library.get("folders")
        for folder in folders if isinstance(folders, list) else []:
            base = _norm(str(folder))
            inside = root == base or root.startswith(base.rstrip("/") + "/")
            if inside or base.startswith(root.rstrip("/") + "/"):
                found.append(int(library["id"]))
                break
    return found


def library_is_dedicated(library: Mapping[str, Any], remote_root: str) -> bool:
    """Every folder of the library is the root of the shelf or lies inside it: reading the library
    is reading that shelf and nothing else (a family library, ADR-0027, EF-EXP-047)."""
    root = _norm(remote_root)
    folders = library.get("folders")
    if not isinstance(folders, list) or not folders:
        return False
    return all(
        (base := _norm(str(folder))) == root or base.startswith(root.rstrip("/") + "/")
        for folder in folders
    )
