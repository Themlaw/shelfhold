"""ENF-COMP-001 (coupable): `known_bad_versions` warns, it never blocks."""

from __future__ import annotations

import pytest

from shelfhold.export.domain.kavita_compat import (
    KNOWN_BAD_VERSIONS,
    KavitaVersion,
    KnownBad,
    TargetMode,
    assess_version,
)
from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.fakes_clock import ManualClock
from tests.kavita_env import NOW, RecordingEvents, run


@pytest.mark.req("ENF-COMP-001")
def test_known_bad_kavita_version_warns_only() -> None:
    advisory = KnownBad("TEST-ADVISORY", "cve", below=KavitaVersion((0, 9, 1, 9)))
    assessment = assess_version("0.9.1.4", known_bad=(advisory,))
    assert assessment.mode is TargetMode.FULL and assessment.known_bad == (advisory,)
    fake, events = FakeKavita(version="0.9.1.4"), RecordingEvents()
    client = KavitaClient(
        build_fetcher(fake), BASE_URL, AdminKey(API_KEY),
        events=events, clock=ManualClock(NOW), known_bad=(advisory,),
    )  # fmt: skip
    run(client.probe())
    assert client.health.mode is TargetMode.FULL
    assert client.health.warnings == ("TEST-ADVISORY (cve)",)
    assert events.codes() == []  # a warning, not an operating event
    run(client.scan_folder("/manga/Series A"))  # nothing is blocked
    assert client.preview_verified
    assert fake.scans == ["/manga/Series A"]
    assert assess_version("0.9.1.9", known_bad=(advisory,)).known_bad == ()


@pytest.mark.req("ENF-COMP-001")
def test_known_bad_kavita_cve_2026_47202_warns() -> None:
    entries = {k.advisory: k for k in KNOWN_BAD_VERSIONS}
    assert entries["CVE-2026-47202"].category == "auth"  # account takeover, fixed in 0.9.0.2
    assert {k.category for k in KNOWN_BAD_VERSIONS} >= {"auth", "access_control"}
    hit = assess_version("0.9.0.1")
    assert "CVE-2026-47202" in {k.advisory for k in hit.known_bad}
    assert "CVE-2026-47202" not in {k.advisory for k in assess_version("0.9.0.2").known_bad}
    assert "access_control" in {k.category for k in assess_version("0.9.0.2").known_bad}
    assert assess_version("0.9.1").known_bad == () == assess_version("0.9.1.4").known_bad
    # the warning reaches the health document of the target
    fake = FakeKavita(version="0.9.0.1")
    client = KavitaClient(
        build_fetcher(fake), BASE_URL, AdminKey(API_KEY),
        events=RecordingEvents(), clock=ManualClock(NOW),
    )  # fmt: skip
    run(client.probe())
    assert any("CVE-2026-47202" in w for w in client.health.warnings)
