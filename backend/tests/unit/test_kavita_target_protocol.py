"""Review of PR #69, minor 7: the Kavita destination keeps the published SDK calls."""

import pytest

from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.sdk.export_target import ExportTarget


@pytest.mark.req("EF-EXP-006")
def test_kavita_destination_keeps_the_call_methods_of_the_sdk_protocol() -> None:
    calls = {"parse_name_call", "inventory_calls", "ensure_library_call", "scan_calls",
             "supported_versions"}  # fmt: skip
    assert all(hasattr(ExportTarget, name) for name in calls)  # the protocol still declares them
    assert all(hasattr(KavitaDestination, name) for name in calls)
