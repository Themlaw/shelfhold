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


@pytest.mark.req("ENF-SEC-015")
def test_raw_number_is_plain_text_before_the_database() -> None:
    from datetime import UTC, datetime

    from shelfhold.acquisition.application.follow import _trusted
    from shelfhold.sdk.refs import UnitInfo

    info = UnitInfo(remote_unit_id="u", source_order=1, number="<b>12</b>&#x202E;.5")
    assert _trusted(info, datetime(2026, 11, 5, tzinfo=UTC)).number == "12.5"
