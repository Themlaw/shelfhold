"""The simulated sources of the M2 journeys, behind one socket pair (80 plain, 443 TLS) in the
network namespace of the stack. One server answers by the host name of the request, the way the
real servers would:

- the catalogue and the mirror of a books project (CSV feed, EPUB per number);
- an encyclopedia's API and its export tool (TLS);
- a patron-less per-book address of an ebook publisher (TLS);
- a generic OPDS catalogue that serves the EPUB files of the library volume (what Shelfhold
  exported, so that the journey can bring its own export back).

Every title, author and byte is made up. `GET /_fake/state` (host `127.0.0.1`) is the test's
window: what was asked, by host. Nothing here is a copy of a third-party file.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from books import book  # noqa: E402
from httpshell import make_server  # noqa: E402

from tests.fakes.opds import acquisition_entry, atom_feed, kavita_description, navigation_entry  # noqa: E402
from tests.fakes.opds.feeds import ATOM, OPENSEARCH, link  # noqa: E402
from tests.fakes.wikisource import Page, info_answer, search_answer  # noqa: E402

LIBRARY = Path(os.environ.get("FAKE_LIBRARY_ROOT", "/library"))
PKI = Path("/pki")
MIRROR, CATALOG = "aleph.gutenberg.org", "catalog.example"
WIKI, EXPORT, PUBLISHER, OPDS = (
    "fr.wikisource.org", "ws-export.wmcloud.org", "standardebooks.org", "opds.example",
)  # fmt: skip
SEEN: list[str] = []
Reply = tuple[int, dict[str, str], bytes]

CSV = (
    "Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves\n"
    '1001,Text,2001-01-01,A Study in Test,en,"Doe, Jane, 1850-1920",Tests,PR,\n'
    '3001,Text,2002-02-02,The Protected Tale,en,"Roe, Richard, 1900-1960",Tests,PR,\n'
    '5001,Sound,2004-04-04,An Audio Reading,en,"Doe, Jane, 1850-1920",,,\n'
)
GUTENBERG = {
    "/cache/epub/1001/pg1001.epub": book(
        "A Study in Test", author="Jane Doe", body="<p>texte</p><script>alert(1)</script>"
    ),  # hostile on purpose: the neutralizer must strip it
    "/cache/epub/3001/pg3001.epub": book("The Protected Tale", author="Richard Roe"),
}
PAGES = {"Le Comte de Test": Page("Le Comte de Test", contributors=("Alice Synth", "Bruno Synth"))}
WIKI_EPUB = {"Le Comte de Test": book("Le Comte de Test", author="Auteur Synth", lang="fr")}
SLUG = "jane-synth/le-test"
PUBLISHER_EPUB = {
    f"/ebooks/{SLUG}/downloads/jane-synth_le-test.epub": book("Le Test Édité", author="Jane Synth")
}


def _ok(body: bytes, mime: str) -> Reply:
    return 200, {"content-type": mime}, body


def gutenberg(host: str, path: str) -> Reply:
    if host == CATALOG and path == "/cache/epub/feeds/pg_catalog.csv":
        return _ok(CSV.encode(), "text/csv")
    if host == MIRROR and path in GUTENBERG:
        return _ok(GUTENBERG[path], "application/epub+zip")
    return 404, {}, b""


def wikisource(host: str, path: str, query: dict[str, str]) -> Reply:
    if host == WIKI and path == "/w/api.php":
        if query.get("list") == "search":
            needle = query.get("srsearch", "").casefold()
            found = [p for p in PAGES.values() if needle in p.title.casefold()]
            return _ok(search_answer(found), "application/json")
        title = query.get("titles", "").replace("_", " ")
        return _ok(info_answer(PAGES.get(title), title), "application/json")
    if host == EXPORT and path == "/":
        title = query.get("page", "").replace("_", " ")
        if title in WIKI_EPUB:
            return _ok(WIKI_EPUB[title], "application/epub+zip")
    return 404, {}, b""


def library_epubs() -> list[Path]:
    return sorted(p for p in LIBRARY.rglob("*.epub") if "/." not in str(p))


def opds(path: str, query: dict[str, str]) -> Reply:
    """A catalogue shaped like a household server's: root, search, a series, its files."""
    root = "/opds"
    if path == root:
        feed = atom_feed("urn:e2e:root", "Catalogue",
                         [navigation_entry("urn:e2e:all", "Séries", f"{root}/series")],
                         links=[link("search", f"{root}/search", OPENSEARCH)])  # fmt: skip
        return _ok(feed, ATOM)
    if path == f"{root}/search":
        return _ok(kavita_description(f"{root}/series?query={{searchTerms}}"), OPENSEARCH)
    if path == f"{root}/series":
        found = [navigation_entry("urn:e2e:series:1", "Exportés", f"{root}/series/1")]
        return _ok(atom_feed("urn:e2e:found", "Résultats", found), ATOM)
    if path == f"{root}/series/1":
        entries = [
            acquisition_entry(f"urn:e2e:book:{n}", f"Exporté {n}", f"{root}/file/{n}",
                              "application/epub+zip")
            for n, _ in enumerate(library_epubs(), start=1)
        ]  # fmt: skip
        return _ok(atom_feed("urn:e2e:series:1", "Exportés", entries), ATOM)
    if path.startswith(f"{root}/file/"):
        files = library_epubs()
        index = int(path.rsplit("/", 1)[1]) - 1
        if 0 <= index < len(files):
            return _ok(files[index].read_bytes(), "application/epub+zip")
    return 404, {}, b""


def publisher(path: str) -> Reply:
    if path in PUBLISHER_EPUB:
        return _ok(PUBLISHER_EPUB[path], "application/epub+zip")
    return 404, {}, b""


def handle(method: str, target: str, headers: dict[str, str], body: bytes) -> Reply:
    host = headers.get("host", "").split(":")[0].lower()
    split = urlsplit(target)
    path, query = split.path, {k: v[0] for k, v in parse_qs(split.query).items()}
    if host in ("127.0.0.1", "localhost") and path == "/_fake/state":
        return _ok(json.dumps({"seen": SEEN}).encode(), "application/json")
    SEEN.append(f"{host} {method} {path}")
    if path == "/robots.txt":
        return 404, {}, b""
    if host in (CATALOG, MIRROR):
        return gutenberg(host, path)
    if host in (WIKI, EXPORT):
        return wikisource(host, path, query)
    if host == PUBLISHER:
        return publisher(path)
    if host == OPDS:
        return opds(path, query)
    return 404, {}, quote(host).encode()


def main() -> None:
    plain = make_server("0.0.0.0", 80, handle, "fake-sources")  # noqa: S104
    tls = make_server(
        "0.0.0.0",
        443,
        handle,
        "fake-sources-tls",
        tls=(str(PKI / "server.pem"), str(PKI / "server.key")),  # noqa: S104
    )
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    plain.serve_forever()


if __name__ == "__main__":
    main()
