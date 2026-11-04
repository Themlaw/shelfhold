"""The golden suite runs: the redaction of a fixed log event is pinned by a syrupy snapshot."""

import pytest
from syrupy.assertion import SnapshotAssertion

from shelfhold.infrastructure.logging import _redact


@pytest.mark.req("ENF-OBS-001")
def test_log_event_redaction_golden(snapshot: SnapshotAssertion) -> None:
    event = {
        "event": "login",
        "user": "alice",
        "api_key": "sk-123",
        "Authorization": "Bearer abc",
        "set_cookie": "sid=1",
        "count": 3,
    }
    assert _redact(None, "info", event) == snapshot
