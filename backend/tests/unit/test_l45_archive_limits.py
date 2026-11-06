"""L45, ENF-SEC-003: the default archive caps are the ones of the spec, and the real receiving
path (`PassthroughStore.ingest`) enforces them."""

import pytest

from shelfhold.infrastructure.archives.safe_open import DEFAULT_LIMITS, ArchiveLimits


@pytest.mark.req("ENF-SEC-003")
def test_archive_default_caps_match_the_spec() -> None:
    assert ArchiveLimits() == DEFAULT_LIMITS
    assert DEFAULT_LIMITS.max_entries == 2_000
    assert DEFAULT_LIMITS.max_total_bytes == 2 * 1024**3
    assert DEFAULT_LIMITS.max_ratio == 100
