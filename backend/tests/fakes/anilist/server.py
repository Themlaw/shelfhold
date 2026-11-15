"""A simulated AniList (GraphQL) replaying the fabricated cassettes of `cassettes/anilist/`."""

from __future__ import annotations

from tests.fakes.cassette_server import CASSETTES, CassetteServer

HOST = "graphql.anilist.co"
TITLE = "Where the Moon Never Reaches"  # invented


def fake_anilist(name: str = "search_degraded", **options: object) -> CassetteServer:
    return CassetteServer(HOST, (CASSETTES / "anilist" / f"{name}.json",), **options)  # type: ignore[arg-type]
