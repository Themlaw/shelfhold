# SPDX-License-Identifier: MIT
"""pytest tool for plugin authors: `pytest_plugins = ["shelfhold.sdk.testing.pytest_plugin"]`."""

from collections.abc import Callable

import pytest

from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import Source
from shelfhold.sdk.testing.destination_kit import assert_destination_conforms
from shelfhold.sdk.testing.fakes import FakeFetcher
from shelfhold.sdk.testing.kit import ConformanceReport, assert_conforms


@pytest.fixture
def offline_fetcher() -> FakeFetcher:
    """An empty scripted Fetcher: every request fails until the test scripts it."""
    return FakeFetcher()


@pytest.fixture
def check_conformance() -> Callable[..., ConformanceReport]:
    """`check_conformance(source, fetcher, reinstall=..., series_ref=...)` runs the kit."""

    def run(
        source: Source,
        fetcher: FakeFetcher,
        *,
        reinstall: Callable[[], Source] | None = None,
        series_ref: SeriesRef | None = None,
    ) -> ConformanceReport:
        return assert_conforms(source, fetcher, reinstall=reinstall, series_ref=series_ref)

    return run


@pytest.fixture
def check_destination() -> Callable[..., ConformanceReport]:
    """`check_destination(destination, DestinationScenario(key=..., plan=...))` runs the
    destination chapter of the kit and raises on the first report that is not clean."""
    return assert_destination_conforms
