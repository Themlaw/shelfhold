"""Review of PR #69, minor 7: the Kavita destination keeps the published SDK calls."""

import pytest
from shelfhold_sdk.export_target import ExportTarget

from shelfhold.export.targets.kavita.destination import KavitaDestination

KAVITA_ONLY = {"parse_name_call", "ensure_library_call", "resolve_remote_calls",
               "event_subscription", "progress_calls"}  # fmt: skip


@pytest.mark.req("EF-EXP-006")
def test_kavita_destination_keeps_the_call_methods_of_the_sdk_protocol() -> None:
    calls = {"inventory_calls", "scan_calls", "supported_versions"}  # fmt: skip
    assert all(hasattr(ExportTarget, name) for name in calls)  # the protocol still declares them
    assert all(hasattr(KavitaDestination, name) for name in calls)
    # SDK 2.0 (M6 L02) removed the Kavita-only members from the published protocol; the host's own
    # destination class keeps the ones it implements, it does not go through the Protocol
    assert not any(hasattr(ExportTarget, name) for name in KAVITA_ONLY)
    assert hasattr(KavitaDestination, "parse_name_call")
    assert hasattr(KavitaDestination, "ensure_library_call")
