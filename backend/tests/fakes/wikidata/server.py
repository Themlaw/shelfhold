"""A simulated Wikidata SPARQL service replaying the cassettes of `cassettes/wikidata`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

HOST = "query.wikidata.org"
TITLE = "The Synthetic Chronicle"  # invented
TWIN = "Twin Title"  # two items bear this label
HOSTILE = "Hostile Item"  # an item whose identifiers are not what they claim
FOREIGN = "Foreign Item"  # an item that is not one of Wikidata's
ABSENT = "Nothing Recorded"
QID = "Q900001"


def fake_wikidata(name: str = "search", **options: object) -> CassetteServer:
    return CassetteServer(HOST, (CASSETTES / "wikidata" / f"{name}.json",), **options)  # type: ignore[arg-type]
