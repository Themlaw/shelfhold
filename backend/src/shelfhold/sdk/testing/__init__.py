# SPDX-License-Identifier: MIT
"""Conformance kit and fakes for plugin authors (ENF-QUAL-003).

The kit runs offline, on synthetic fixtures only (`record_mode = "none"`): any request that is not
scripted fails. It is delivered as a pytest tool inside the SDK and published with `shelfhold-sdk`.
"""

from shelfhold.sdk.testing.fakes import (
    FakeFetcher,
    FakeRangedConnectedService,
    NetworkForbidden,
    fake_ranged_fetcher,
)
from shelfhold.sdk.testing.kit import (
    CheckResult,
    ConformanceReport,
    assert_conforms,
    run_source_conformance,
)

__all__ = [
    "CheckResult",
    "ConformanceReport",
    "FakeFetcher",
    "FakeRangedConnectedService",
    "NetworkForbidden",
    "assert_conforms",
    "fake_ranged_fetcher",
    "run_source_conformance",
]
