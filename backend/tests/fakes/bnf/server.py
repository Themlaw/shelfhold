"""A simulated BnF (SRU and SPARQL) replaying the fabricated cassettes of `cassettes/bnf/`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

SRU_HOST = "catalogue.bnf.fr"
SPARQL_HOST = "data.bnf.fr"
TITLE = "The Synthetic Chronicle"  # invented
ISBN = "9780306406157"  # a valid ISBN-13 that names no real book here
SPARQL_ISBN = "9781402894626"  # the one only the SPARQL door knows
ABSENT = "Nothing Recorded"


def fake_bnf(name: str = "search", **options: object) -> CassetteServer:
    return CassetteServer(
        SRU_HOST,
        (CASSETTES / "bnf" / f"{name}.json",),
        extra_hosts=(SPARQL_HOST,),
        **options,  # type: ignore[arg-type]
    )
