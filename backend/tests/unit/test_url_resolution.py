"""EF-SRC-004: a pasted URL is resolved by the first active plugin whose `url_patterns` match.

A pattern is `<scheme>://<host pattern><path pattern>`: the host pattern has the DomainRule grammar
(exact, `.suffix`, `*.joker`), `*` in the path stands for one segment and `**` for any number.
No match, or a match that resolves to nothing, is the 422 `source.no_match` ("aucune source").
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

from shelfhold.i18n import load_catalog
from shelfhold.interfaces.http_api.catalog import all_error_codes
from shelfhold.plugin_host.errors import NoSourceForUrl
from shelfhold.plugin_host.registry import (
    PluginCandidate,
    PluginOrigin,
    PluginRegistry,
    PluginStatus,
    UrlPatternError,
    compile_url_pattern,
)
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService

pytestmark = pytest.mark.req("EF-SRC-004")


class _Plugin:
    def __init__(self, key: str, patterns: list[str], *, capabilities: list[str] | None = None):
        raw = FakeRangedConnectedService().manifest.model_dump(mode="json")
        self.manifest = {
            **raw,
            "key": key,
            "url_patterns": patterns,
            "capabilities": capabilities or ["SEARCH", "URL_RESOLVE"],
        }
        self.calls: list[str] = []

    async def resolve_url(self, f: Any, url: str) -> SeriesRef | None:
        self.calls.append(url)
        return SeriesRef(source_key=self.manifest["key"], remote_id="series/42", title="Synthetic")


def _registry(*plugins: _Plugin) -> PluginRegistry:
    registry = PluginRegistry()
    registry.load_all(
        PluginCandidate(key=p.manifest["key"], origin=PluginOrigin.FIRST_PARTY, load=lambda p=p: p)
        for p in plugins
    )
    return registry


def _resolve(registry: PluginRegistry, url: str) -> Any:
    return asyncio.run(registry.resolve_url(url, lambda record: None))


def test_url_patterns_resolve_to_source() -> None:
    """Shelfhold form of `test_url_patterns_resolve_to_source`."""
    other = _Plugin("other", ["https://books.example.net/**"])
    target = _Plugin("target", ["https://*.example.org/series/*", "https://example.org/s/*"])
    registry = _registry(other, target)
    resolved = _resolve(registry, "https://www.example.org/series/42?page=2")
    assert resolved.key == "target"
    assert isinstance(resolved.ref, SeriesRef) and resolved.ref.remote_id == "series/42"
    assert target.calls == ["https://www.example.org/series/42?page=2"] and other.calls == []


def test_first_matching_plugin_wins() -> None:
    first = _Plugin("first", ["https://example.org/**"])
    second = _Plugin("second", ["https://example.org/**"])
    assert _resolve(_registry(first, second), "https://example.org/a").key == "first"
    assert second.calls == []


def test_unresolved_url_returns_422_no_source() -> None:
    registry = _registry(_Plugin("only", ["https://example.org/series/*"]))
    for url in (
        "https://evil.test/series/1",
        "https://example.org/other/1",
        "https://example.org.evil/series/1",
        "https://user:pw@example.org/series/1",
        "javascript:alert(1)",
        "not a url",
    ):
        with pytest.raises(NoSourceForUrl) as caught:
            _resolve(registry, url)
        assert caught.value.code == "source.no_match"
    spec = {c.code: c for c in all_error_codes()}["source.no_match"]
    assert spec.status == 422 and spec.audience == "user"
    assert "aucune source" in load_catalog("fr")[spec.title_key].lower()


def test_match_that_resolves_to_nothing_is_no_source() -> None:
    class Empty(_Plugin):
        async def resolve_url(self, f: Any, url: str) -> SeriesRef | None:
            return None

    with pytest.raises(NoSourceForUrl):
        _resolve(_registry(Empty("empty", ["https://example.org/**"])), "https://example.org/x")


def test_plugin_without_url_resolve_capability_is_skipped() -> None:
    mute = _Plugin("mute", ["https://example.org/**"], capabilities=["SEARCH"])
    with pytest.raises(NoSourceForUrl):
        _resolve(_registry(mute), "https://example.org/x")
    assert mute.calls == []


def test_resolution_runs_under_the_audit_hook(tmp_path: Path) -> None:
    class Chatty(_Plugin):
        async def resolve_url(self, f: Any, url: str) -> SeriesRef | None:
            (tmp_path / "x").write_text("x", encoding="utf-8")
            return await super().resolve_url(f, url)

    registry = _registry(Chatty("chatty", ["https://example.org/**"]))
    with structlog.testing.capture_logs() as logs:
        assert _resolve(registry, "https://example.org/x").key == "chatty"
    assert any(e["event"] == "plugin.forbidden_io" and e["plugin_id"] == "chatty" for e in logs)


def test_no_source_error_does_not_name_the_plugin() -> None:
    class Empty(_Plugin):
        async def resolve_url(self, f: Any, url: str) -> SeriesRef | None:
            return None

    registry = _registry(Empty("secret-plugin-key", ["https://example.org/**"]))
    with structlog.testing.capture_logs() as logs, pytest.raises(NoSourceForUrl) as caught:
        _resolve(registry, "https://example.org/x")
    assert "secret-plugin-key" not in str(caught.value)
    assert any(e.get("plugin_id") == "secret-plugin-key" for e in logs)  # the admin's log


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("https://example.org/series/*", "https://example.org/series/42", True),
        ("https://example.org/series/*", "https://example.org/series/42/ch/1", False),
        ("https://example.org/series/**", "https://example.org/series/42/ch/1", True),
        ("https://example.org/series/*", "http://example.org/series/42", False),
        ("*://example.org/series/*", "http://example.org/series/42", True),
        ("https://*.example.org/**", "https://example.org.evil/x", False),
        ("https://*.example.org/**", "https://evilexample.org/x", False),
        ("https://*.example.org/**", "https://A.Example.ORG./x", True),
        ("https://bücher.example/**", "https://xn--bcher-kva.example/x", True),
        ("https://example.org", "https://example.org/anything/at/all", True),
        ("https://example.org/a.b", "https://example.org/aXb", False),  # literal, not a regex
    ],
)
def test_url_pattern_matching(pattern: str, url: str, expected: bool) -> None:
    assert compile_url_pattern(pattern).matches(url) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "example.org/x",
        "ftp://example.org/x",
        "https://*.com/x",
        "https://example.org?q=*",
        "https://example.org/*/*/*/**/*",  # a pattern built to make matching slow
        "",
    ],
)
def test_invalid_url_patterns_are_refused(pattern: str) -> None:
    with pytest.raises(UrlPatternError):
        compile_url_pattern(pattern)


def test_plugin_with_an_invalid_pattern_or_rule_is_incompatible() -> None:
    bad_pattern = _Plugin("bad-pattern", ["https://*.com/**"])
    bad_rule = _Plugin("bad-rule", [])
    bad_rule.manifest["permissions"] = {"domains": [{"pattern": "*", "purpose": "api"}]}
    records = PluginRegistry().load_all(
        PluginCandidate(key=p.manifest["key"], origin=PluginOrigin.FIRST_PARTY, load=lambda p=p: p)
        for p in (bad_pattern, bad_rule)
    )
    assert [r.status for r in records] == [PluginStatus.INCOMPATIBLE] * 2
    assert "url_patterns" in (records[0].reason or "")
    assert "domain" in (records[1].reason or "")
