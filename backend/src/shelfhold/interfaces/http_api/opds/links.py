"""What a link of the feed may be (ADR-0053 §4, §8).

Every href is a path from the root of the site: it begins with the mount prefix (`root_path`, or
the `X-Forwarded-Prefix` of a trusted proxy) and `/opds`, and nothing else. It is never built from
`Host`, never carries a scheme, a user name, a credential or a title, and the prefix is a plain
path or nothing. A file name is made of the title of what a reader may see, cleaned: no path
separator, no control character, never `..`.
"""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

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


class Paths:
    """The hrefs of the feed, all under `{prefix}/opds`."""

    def __init__(self, prefix: str) -> None:
        self.base = f"{prefix}/opds"

    def _with(self, path: str, **query: object) -> str:
        wanted = {k: v for k, v in query.items() if v is not None}
        return f"{self.base}{path}" + (f"?{urlencode(wanted)}" if wanted else "")

    def root(self) -> str:
        return self.base

    def opensearch(self) -> str:
        return self._with("/opensearch.xml")

    def shelves(self) -> str:
        return self._with("/shelves")

    def shelf(self, shelf_id: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._with(f"/shelves/{shelf_id}", **_page(offset, limit))

    def series_list(self, *, offset: int = 0, limit: int | None = None) -> str:
        return self._with("/series", **_page(offset, limit))

    def series(self, series_id: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._with(f"/series/{series_id}", **_page(offset, limit))

    def works(self, *, offset: int = 0, limit: int | None = None) -> str:
        return self._with("/works", **_page(offset, limit))

    def recent(self, *, offset: int = 0, limit: int | None = None) -> str:
        return self._with("/recent", **_page(offset, limit))

    def work(self, work_id: str) -> str:
        return self._with(f"/works/{work_id}")

    def search(self, q: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._with("/search", q=q, **_page(offset, limit))

    def search_template(self) -> str:
        return f"{self.base}/search?q={{searchTerms}}"

    def cover(self, content_hash: str, size: int) -> str:
        return self._with(f"/covers/{content_hash}/{size}")

    def complete(self, edition_id: str, part: int = 1) -> str:
        return self._with(f"/editions/{edition_id}/complete", part=part if part > 1 else None)

    def bundle(self, version_id: str) -> str:
        return self._with(f"/bundles/{version_id}")


def _page(offset: int, limit: int | None) -> dict[str, int | None]:
    return {"offset": offset or None, "limit": limit}
