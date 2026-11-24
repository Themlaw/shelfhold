"""An isolated plugin that dies mid-call becomes a coded problem, never a 500 (EF-SRC-016)."""

from datetime import UTC, datetime

import pytest

from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.errors import CodedRefusal
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


class _GoneComposition:
    """A composition whose isolated plugin died: every call into it raises `PluginFailed`."""

    class clock:
        @staticmethod
        def now() -> datetime:
            return datetime(2026, 11, 24, tzinfo=UTC)

    def run(self, work: object) -> object:
        raise PluginFailed("the plugin's connection broke")


class _GoneAcquirer:
    def filters(self, source_key: str) -> object:
        raise PluginFailed("the plugin's connection broke")


@pytest.mark.req("EF-SRC-016")
def test_a_dead_plugin_is_refused_by_the_acquire_doors_under_plugin_failed() -> None:
    ops = AcquireOps(_GoneComposition())  # type: ignore[arg-type]
    with pytest.raises(CodedRefusal) as run:
        ops._run(object())
    assert run.value.code == "plugin.failed"
    ops.acquirer = lambda **_: _GoneAcquirer()  # type: ignore[method-assign,assignment,return-value]
    with pytest.raises(CodedRefusal) as filters:
        ops.filters("sample:catalogue")
    assert filters.value.code == "plugin.failed"
