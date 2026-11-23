"""Relecture adverse de L22 : le relais de scan garde une seule liaison par serveur Komga.

`relay_komga_scans` déduplique par `client.base_url` : deux arbres écrits dans la même passe,
indexés par le même Komga dans deux bibliothèques, ne donnent qu'un scan ; la seconde
bibliothèque ne voit pas les nouveaux livres (et sa corbeille n'est pas vidée).
"""

from __future__ import annotations

from typing import Any

import pytest

import shelfhold.runtime_jobs as rj


class _Client:
    base_url = "http://komga.lan:25600"


@pytest.mark.req("EF-EXP-020")
def test_one_komga_indexing_two_written_trees_scans_both_folders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    indexers = {
        "D1": [("K1", "T", client, "/manga/One")],
        "D2": [("K2", "T", client, "/comics/Two")],
    }
    monkeypatch.setattr(rj, "komga_indexers", lambda _comp, d: indexers[d])
    scanned: list[tuple[str, str]] = []

    async def scan(_comp: Any, _client: Any, binding_id: str, root: str) -> None:
        scanned.append((binding_id, root))

    monkeypatch.setattr(rj, "_scan_komga_library", scan)
    rj.relay_komga_scans(object(), ["D1", "D2"])  # type: ignore[arg-type]
    assert sorted(scanned) == [("K1", "/manga/One"), ("K2", "/comics/Two")]
