"""What a surface that builds its own links and file names shares (M4 L48, M5 L17a): the mount
prefix a link begins with, and a file name made of a title.

The prefix is the one a trusted proxy announced (`X-Forwarded-Prefix`, read by
`ClientIpMiddleware`) or the server's `root_path`: a plain path of a few safe segments or nothing,
never built from `Host`. A file name has no path separator, no control character, never `..`.
"""

import re
from collections.abc import Mapping
from typing import Any

_PREFIX = re.compile(r"(?:/[A-Za-z0-9._~-]{1,64}){1,8}")
_MAX_PREFIX = 128
_FORBIDDEN = set('/\\:*?"<>|')
_MAX_NAME = 200


def _plain(raw: object) -> str | None:
    """`raw` as a mount prefix: `/a/b` without a trailing slash, or None if it is anything else."""
    if not isinstance(raw, str):
        return None
    cleaned = raw[:-1] if raw.endswith("/") and raw != "/" else raw
    if cleaned == "":
        return ""
    if len(cleaned) > _MAX_PREFIX or not _PREFIX.fullmatch(cleaned):
        return None
    if any(segment in {".", ".."} for segment in cleaned.split("/")):
        return None
    return cleaned


def mount_prefix(scope: Mapping[str, Any]) -> str:
    """The prefix of the mount point: the one a trusted proxy announced
    (`state["client_prefix"]`, set by `ClientIpMiddleware`), else the server's `root_path`."""
    state = scope.get("state") or {}
    announced = _plain(state.get("client_prefix")) if isinstance(state, Mapping) else None
    if announced:
        return announced
    return _plain(scope.get("root_path", "")) or ""


def safe_filename(raw: str) -> str:
    """A file name from a title: no separator, no control character, no `..`, never empty."""
    kept = "".join(c for c in raw if c not in _FORBIDDEN and c.isprintable())
    while ".." in kept:
        kept = kept.replace("..", ".")
    kept = kept.strip(chr(32) + ".")
    if len(kept) > _MAX_NAME:
        stem, dot, ext = kept.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            kept = stem[: _MAX_NAME - len(ext) - 1] + "." + ext
        else:
            kept = kept[:_MAX_NAME]
    return kept or "download"
