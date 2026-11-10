# SPDX-License-Identifier: MIT
"""Conformance kit and fakes for plugin authors (ENF-QUAL-003).

The kit runs offline, on synthetic fixtures only (`record_mode = "none"`): any request that is not
scripted fails. It is delivered as a pytest tool inside the SDK and published with `shelfhold-sdk`.
"""

from shelfhold.sdk.testing.fakes import (
    FakeFetcher,
    FakeMetadataProvider,
    FakeRangedConnectedService,
    NetworkForbidden,
    fake_metadata_fetcher,
    fake_ranged_fetcher,
)
from shelfhold.sdk.testing.kit import (
    CheckResult,
    ConformanceReport,
    assert_conforms,
    run_metadata_conformance,
    run_source_conformance,
    run_trapped_epub_corpus,
)

__all__ = [
    "CheckResult",
    "ConformanceReport",
    "FakeFetcher",
    "FakeMetadataProvider",
    "FakeRangedConnectedService",
    "NetworkForbidden",
    "assert_conforms",
    "fake_metadata_fetcher",
    "fake_ranged_fetcher",
    "run_metadata_conformance",
    "run_source_conformance",
    "run_trapped_epub_corpus",
]
