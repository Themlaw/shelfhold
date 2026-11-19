"""What a link of the feed may be (ADR-0053 §4, §8).

Every href is a path from the root of the site: it begins with the mount prefix (`root_path`, or
the `X-Forwarded-Prefix` of a trusted proxy) and `/opds`, and nothing else. It is never built from
`Host`, never carries a scheme, a user name, a credential or a title, and the prefix is a plain
path or nothing. A file name is made of the title of what a reader may see, cleaned: no path
separator, no control character, never `..`.
"""

from urllib.parse import urlencode

from shelfhold.interfaces.http_api.path_safety import mount_prefix, safe_filename

__all__ = ["Paths", "mount_prefix", "safe_filename"]


class Paths:
    """The hrefs of the feed, all under `{prefix}/opds`."""

    def __init__(self, prefix: str, *, json_feed: bool = False) -> None:
        """`json_feed`: the links to the feeds (not to a file, a cover or a page) are those of the
        OPDS 2.0 feed under `/opds/v2`; the others are shared, there is one route for each."""
        self.base = f"{prefix}/opds"
        self.json_feed = json_feed
        self.feeds = f"{self.base}/v2" if json_feed else self.base

    def _with(self, path: str, **query: object) -> str:
        wanted = {k: v for k, v in query.items() if v is not None}
        return f"{self.base}{path}" + (f"?{urlencode(wanted)}" if wanted else "")

    def _feed(self, path: str, **query: object) -> str:
        wanted = {k: v for k, v in query.items() if v is not None}
        return f"{self.feeds}{path}" + (f"?{urlencode(wanted)}" if wanted else "")

    def root(self) -> str:
        return self.feeds

    def opensearch(self) -> str:
        return self._with("/opensearch.xml")

    def shelves(self) -> str:
        return self._feed("/shelves")

    def shelf(self, shelf_id: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed(f"/shelves/{shelf_id}", **_page(offset, limit))

    def series_list(self, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed("/series", **_page(offset, limit))

    def series(self, series_id: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed(f"/series/{series_id}", **_page(offset, limit))

    def works(self, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed("/works", **_page(offset, limit))

    def recent(self, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed("/recent", **_page(offset, limit))

    def selections(self) -> str:
        return self._feed("/selections")

    def selection(self, selection_id: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed(f"/selections/{selection_id}", **_page(offset, limit))

    def work(self, work_id: str) -> str:
        return self._feed(f"/works/{work_id}")

    def search(self, q: str, *, offset: int = 0, limit: int | None = None) -> str:
        return self._feed("/search", q=q, **_page(offset, limit))

    def search_template(self) -> str:
        if self.json_feed:  # a URI template (RFC 6570), as OPDS 2.0 has it
            return f"{self.feeds}/search{{?q}}"
        return f"{self.base}/search?q={{searchTerms}}"

    def cover(self, content_hash: str, size: int) -> str:
        return self._with(f"/covers/{content_hash}/{size}")

    def complete(self, edition_id: str, part: int = 1) -> str:
        return self._with(f"/editions/{edition_id}/complete", part=part if part > 1 else None)

    def kepub(self, item_id: str) -> str:
        return self._with(f"/kepub/{item_id}")

    def pse(self, item_id: str) -> str:
        """The template of a `pse:stream` link: the client puts the page number (from 0) in it."""
        return f"{self.base}/pse/{item_id}/{{pageNumber}}"

    def bundle(self, version_id: str) -> str:
        return self._with(f"/bundles/{version_id}")


def _page(offset: int, limit: int | None) -> dict[str, int | None]:
    return {"offset": offset or None, "limit": limit}
