"""EF-EXP-047, ENF-SEC-016 (M4 L08b): Komga declares library partitioning, and counts for it only
once its accounts were audited; a release below 1.22.0 is a known bad one (category `cve`)."""

from __future__ import annotations

import pytest

from shelfhold.export.domain.bindings import effective_partitioning
from shelfhold.export.domain.komga_access import (
    KNOWN_BAD_VERSIONS,
    KOMGA_FLOOR,
    assess_version,
)
from shelfhold.export.targets.komga import KomgaDestination


@pytest.mark.req("EF-EXP-047")
def test_komga_destination_declares_library_partitioning() -> None:
    assert KomgaDestination.capabilities.access_partitioning == "library"


@pytest.mark.req("EF-EXP-047")
@pytest.mark.parametrize("attested", [True, False])
def test_the_declaration_counts_only_with_the_proof_of_an_audit(attested: bool) -> None:
    """Whatever the declaration and the attestation of an admin: no proof, no partitioning."""
    assert effective_partitioning("komga", "library", attested=attested) == "none"
    assert (
        effective_partitioning("komga", "library", attested=attested, audit_proved=False) == "none"
    )
    assert (
        effective_partitioning("komga", "library", attested=attested, audit_proved=True)
        == "library"
    )
    assert effective_partitioning("cwa", "library", attested=True, audit_proved=True) == "none"
    assert (
        effective_partitioning("plugin_x", "library", attested=False, audit_proved=True) == "none"
    )


@pytest.mark.req("ENF-SEC-016")
def test_komga_below_1_22_in_known_bad_versions() -> None:
    assert str(KOMGA_FLOOR) == "1.22.0"
    assert [(k.category, str(k.below)) for k in KNOWN_BAD_VERSIONS] == [("cve", "1.22.0")]
    for raw in ("1.21.9", "1.0.0", "0.157.2", "v1.21.0"):
        verdict = assess_version(raw)
        assert not verdict.accepted and verdict.known_bad, raw
        assert verdict.reason == "version_below_floor"
    for raw in ("1.22.0", "1.22.1", "1.23.4", "2.0.0"):
        verdict = assess_version(raw)
        assert verdict.accepted and not verdict.known_bad and verdict.reason is None, raw


@pytest.mark.req("ENF-SEC-016", "EF-EXP-047")
@pytest.mark.parametrize(
    "raw", [None, "", "  ", "unknown", "1.22.0-SNAPSHOT", "1", "1.x.0", "9" * 400]
)
def test_an_unknown_or_unreadable_version_is_never_accepted(raw: str | None) -> None:
    verdict = assess_version(raw)
    assert not verdict.accepted and verdict.reason == "version_unknown"
