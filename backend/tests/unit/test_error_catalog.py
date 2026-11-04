"""EF-UX-003 / EF-INT-001: the error catalog is declared per context and aggregated here."""

import importlib
from pathlib import Path

import pytest

from shelfhold.events.operating import (
    M1B,
    OPERATING_EVENTS,
    WAITING_STATE_EVENT,
    WAITING_WITHOUT_EVENT,
)
from shelfhold.events.waiting_state import WaitingState
from shelfhold.i18n import load_catalog
from shelfhold.infrastructure.fetcher import errors as fetch_errors
from shelfhold.interfaces.http_api.catalog import all_error_codes, error_code_modules
from shelfhold.sdk import source as sdk_source

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def _subclasses(base: type) -> list[type]:
    found: list[type] = []
    for sub in base.__subclasses__():
        found.append(sub)
        found.extend(_subclasses(sub))
    return found


@pytest.mark.req("EF-UX-003", "EF-INT-001")
def test_error_catalog_every_error_has_action_fr() -> None:
    catalog = load_catalog("fr")
    codes = all_error_codes()
    assert codes
    for spec in codes:
        assert catalog.get(spec.title_key, "").strip(), f"{spec.code}: no French title"
        assert catalog.get(spec.action_key, "").strip(), f"{spec.code}: no French action"
        assert spec.audience in {"admin", "user"}
        assert 400 <= spec.status <= 599


@pytest.mark.req("EF-UX-003", "EF-INT-001")
def test_error_catalog_codes_unique_and_type_uris_stable() -> None:
    codes = all_error_codes()
    assert len({c.code for c in codes}) == len(codes), "a code is declared twice"
    assert len({c.type_uri for c in codes}) == len(codes), "two codes share a type URI"
    by_code = {c.code: c.type_uri for c in codes}
    # The URI is part of the public contract: renaming a code breaks clients.
    assert by_code["reauth_required"] == "/api/v1/problems/reauth-required"
    assert by_code["credential_not_allowed"] == "/api/v1/problems/credential-not-allowed"
    assert by_code["resync_required"] == "/api/v1/problems/resync-required"
    assert by_code["source.dns_blocked"] == "/api/v1/problems/source.dns-blocked"


@pytest.mark.req("EF-UX-003", "EF-INT-006")
def test_error_catalog_has_a_code_for_every_ef_int_006_event() -> None:
    """Both ways: every event has its code, and every failure-matrix code is an event."""
    codes = all_error_codes()
    declared = {c.code for c in codes}
    flagged = {c.code for c in codes if c.event}
    assert OPERATING_EVENTS - declared == set(), "event without a code"
    assert flagged - OPERATING_EVENTS == set(), "failure-matrix code that is not an event"
    assert OPERATING_EVENTS - flagged == set(), "event whose code is not flagged"
    assert M1B <= OPERATING_EVENTS
    # Follow events (Q3e) and the late additions are present.
    for name in (
        "source.fallback",
        "series.gap",
        "source.instance_changed",
        "tombstone.journal_truncated",
    ):
        assert name in OPERATING_EVENTS


@pytest.mark.req("EF-UX-003", "EF-INT-023")
def test_every_waiting_state_has_its_event_or_is_declared_without() -> None:
    assert set(WAITING_STATE_EVENT) | WAITING_WITHOUT_EVENT == set(WaitingState)
    assert not set(WAITING_STATE_EVENT) & WAITING_WITHOUT_EVENT
    assert set(WAITING_STATE_EVENT.values()) <= OPERATING_EVENTS
    assert len(set(WAITING_STATE_EVENT.values())) == len(WAITING_STATE_EVENT)  # one event each


@pytest.mark.req("EF-UX-003")
def test_job_ledger_error_codes_are_in_the_catalog() -> None:
    from shelfhold.jobs.ledger import PREEMPTED

    declared = {c.code for c in all_error_codes()}
    assert {PREEMPTED, "job.unknown_kind", "job.failed"} <= declared


@pytest.mark.req("EF-UX-003")
def test_error_catalog_covers_every_typed_exception_code() -> None:
    declared = {c.code for c in all_error_codes()}
    raised = {
        str(getattr(cls, "code"))  # noqa: B009
        for base in (fetch_errors.FetchError, sdk_source.SourceError)
        for cls in [base, *_subclasses(base)]
    }
    assert raised - declared == set()


@pytest.mark.req("EF-UX-003")
def test_every_context_errors_module_is_aggregated() -> None:
    on_disk = {
        ".".join(p.relative_to(SRC.parent).with_suffix("").parts)
        for p in SRC.rglob("errors.py")
        if "errors" in p.name and hasattr(importlib.import_module(
            ".".join(p.relative_to(SRC.parent).with_suffix("").parts)), "CODES")
    }  # fmt: skip
    assert on_disk == set(error_code_modules())
