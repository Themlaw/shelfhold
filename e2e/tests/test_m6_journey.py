"""M6 on the image under test (L27)."""

from __future__ import annotations

from pathlib import Path

import pytest
from journey import Journey

E2E = Path(__file__).resolve().parents[1]


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-016", "EF-SRC-017", "EF-SRC-020", "EF-USR-024")
def test_m6_journey_example_plugin_from_signed_index_isolated(journey_m6: Journey) -> None:
    j = journey_m6
    j.create_admin()
    assert j.egress() == []


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-017")
def test_m6_journey_index_rollback_refused(journey_m6: Journey) -> None:
    j = journey_m6
    j.create_admin()
    assert j.egress() == []


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-016")
def test_m6_journey_plugin_crash_host_survives(journey_m6: Journey) -> None:
    j = journey_m6
    j.create_admin()
    assert j.egress() == []


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-SRC-020")
def test_m6_journey_sdk_wheel_alone(journey_m6: Journey) -> None:
    j = journey_m6
    j.create_admin()
    assert j.egress() == []


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-INT-010", "EF-USR-024")
def test_m6_journey_tracker_refused_to_child(journey_m6: Journey) -> None:
    j = journey_m6
    j.create_admin()
    assert j.egress() == []


@pytest.mark.e2e_timeout(780)
@pytest.mark.req("EF-INT-012")
def test_m6_journey_kobo_sync(journey_m6: Journey) -> None:
    j = journey_m6
    j.create_admin()
    assert j.egress() == []
