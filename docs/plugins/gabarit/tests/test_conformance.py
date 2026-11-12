"""Le test que vous gardez : la source passe le kit de conformité, hors réseau."""

from pathlib import Path

from plugin_gabarit.source import ExampleSource
from shelfhold.sdk.testing import FakeFetcher, assert_conforms

CASSETTE = Path(__file__).parent / "cassettes" / "catalogue.json"


def test_the_example_source_conforms() -> None:
    fetcher = FakeFetcher.from_cassette(CASSETTE)
    assert_conforms(ExampleSource(), fetcher)
    assert fetcher.unscripted == []  # rien n'a tenté de sortir du script
