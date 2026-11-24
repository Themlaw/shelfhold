"""An isolated plugin that dies mid-call becomes a coded problem, never a 500 (EF-SRC-016)."""

import pytest

from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.plugin_host.rpc.protocol import PluginFailed


@pytest.mark.req("EF-SRC-016")
def test_plugin_failed_becomes_a_coded_problem() -> None:
    with pytest.raises(ProblemError) as raised, refusals():
        raise PluginFailed("the plugin's connection broke: ConnectionResetError")
    assert str(raised.value) == "plugin.failed"
    assert raised.value.spec.code == "plugin.failed"
    assert raised.value.status == 502


@pytest.mark.req("EF-SRC-016")
def test_plugin_failed_does_not_leak_plugin_text() -> None:
    # the message carried by the exception must never reach the client
    with pytest.raises(ProblemError) as raised, refusals():
        raise PluginFailed("secret trace from the plugin")
    assert "secret trace from the plugin" not in str(raised.value)
    assert raised.value.detail is None or "secret trace" not in (raised.value.detail or "")
