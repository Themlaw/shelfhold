"""M2 L24 (EF-SRC-011, ENF-LEG-003, EF-BIB-033): the manifest of the Wikisource source, the way it
reads a pasted address and the attribution it builds. No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shelfhold.plugin_host.adapters.native.wikisource import WikisourceSource, build_manifest
from shelfhold.plugin_host.registry import compile_url_pattern
from shelfhold.sdk.testing.fakes import FakeFetcher

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003")
def test_the_manifest_file_is_the_one_the_source_declares() -> None:
    on_disk = json.loads((ROOT / "sources/wikisource/manifest.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(build_manifest().model_dump_json())


@pytest.mark.req("EF-SRC-011", "ENF-LEG-003", "EF-BIB-033", "EF-BIB-051")
def test_the_manifest_declares_its_terms_licence_trust_and_the_two_places_it_may_ask() -> None:
    m = build_manifest()
    assert m.terms_url and m.respects_robots and m.attribution and "CC BY-SA" in m.attribution
    assert m.content_trust == "untrusted" and m.rights_basis == "licence"
    assert m.rate_limit.concurrency == 1 and m.rate_limit.requests_per_second <= 1.0
    rules = {(r.pattern, r.path_prefix) for r in m.permissions.domains}
    assert rules == {("*.wikisource.org", "/w/"), ("ws-export.wmcloud.org", None)}
    assert all(r.base_url_setting is None for r in m.permissions.domains)  # no address to swap


@pytest.mark.req("EF-SRC-011", "EF-SRC-004")
def test_the_url_patterns_take_only_the_book_pages_of_a_wikisource() -> None:
    patterns = [compile_url_pattern(p) for p in build_manifest().url_patterns]
    ok = "https://fr.wikisource.org/wiki/Titre"
    assert any(p.matches(ok) for p in patterns)
    for url in (
        "https://fr.wikisource.org/w/api.php",
        "https://fr.wikisource.org.evil.example/wiki/Titre",
        "https://wikisource.org.evil.example/wiki/Titre",
        "https://fr.wikipedia.org/wiki/Titre",
    ):
        assert not any(p.matches(url) for p in patterns)


@pytest.mark.req("EF-SRC-011", "ENF-SEC-014")
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://fr.wikisource.org/wiki/Titre", "fr:Titre"),
        ("https://en.wikisource.org/wiki/The_Title/Chapter_1", "en:The_Title/Chapter_1"),
        ("https://fr.wikisource.org/wiki/Titre%20avec%20blanc", "fr:Titre_avec_blanc"),
        ("https://fr.wikisource.org/wiki/Titre#Section", "fr:Titre"),
        ("https://fr.wikisource.org/wiki/Titre?action=raw", "fr:Titre"),
        ("https://fr.wikisource.org/wiki/", None),
        ("https://fr.wikisource.org/wiki/A%0AB", None),
        ("https://fr.wikisource.org/wiki/A%7CB", None),
        ("https://fr.wikisource.org/wiki/Sp%C3%A9cial:Recherche", None),
        ("https://fr.wikisource.org/wiki/Utilisateur:X", None),
        ("https://FR.WIKISOURCE.ORG/wiki/Titre", "fr:Titre"),
        ("https://wikisource.org/wiki/Titre", None),
        ("ftp://fr.wikisource.org/wiki/Titre", None),
    ],
)
def test_a_pasted_address_names_a_book_or_nothing(url: str, expected: str | None) -> None:
    import asyncio

    ref = asyncio.run(WikisourceSource().resolve_url(FakeFetcher(), url))
    assert (ref.remote_id if ref is not None else None) == expected
