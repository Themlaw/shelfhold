"""EF-SRC-003: the plugin's DomainRule allowlist, applied by the Fetcher at every hop.

Grammar (docs/admin/plugins-tiers.md): `example.org` is that host only; `.example.org` is the
host and every name under it; `*.example.org` is every name under it, never the host itself. A
joker is a whole leftmost label. Names compare in their A-label (punycode) form, case folded, a
single trailing dot ignored. Nothing widens a rule: no substring, no suffix without a label
boundary, no joker that spans a public suffix by itself.
"""

import asyncio
from typing import Any

import httpx2
import pytest
import structlog

from shelfhold.infrastructure.fetcher import GuardedFetcher
from shelfhold.infrastructure.fetcher.errors import DomainNotAllowed
from shelfhold.infrastructure.fetcher.guard import (
    DomainAllowlist,
    DomainPatternError,
    canonical_host,
    compile_domain_pattern,
)
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.sdk.models import DomainRule
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.redaction_support import forget_secret_values, synthetic_text

pytestmark = pytest.mark.req("EF-SRC-003")

PUBLIC = "93.184.216.34"


def _matches(pattern: str, host: str) -> bool:
    return compile_domain_pattern(pattern).matches(host)


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        # exact
        ("example.com", "example.com", True),
        ("example.com", "www.example.com", False),
        ("example.com", "example.com.evil", False),
        ("example.com", "evilexample.com", False),
        # joker: every name under it, never the apex
        ("*.example.com", "a.example.com", True),
        ("*.example.com", "a.b.example.com", True),
        ("*.example.com", "example.com", False),
        ("*.example.com", "example.com.evil", False),
        ("*.example.com", "evilexample.com", False),
        ("*.example.com", "a.example.com.evil", False),
        ("*.example.com", "a-example.com", False),
        ("*.example.com", "example.co", False),
        ("*.cdn.example.org", "img.cdn.example.org", True),
        ("*.cdn.example.org", "cdn.example.org", False),
        ("*.cdn.example.org", "evilcdn.example.org", False),
        # suffix: the apex and every name under it
        (".example.com", "example.com", True),
        (".example.com", "a.b.example.com", True),
        (".example.com", "evilexample.com", False),
        (".example.com", "example.com.evil", False),
        # case and trailing dot, on both sides
        ("Example.COM", "example.com", True),
        ("*.example.com", "A.EXAMPLE.COM", True),
        ("*.example.com", "a.example.com.", True),
        ("example.com.", "example.com", True),
        ("example.com", "example.com..", False),
        # IDN: the A-label form is what compares
        ("bücher.example", "xn--bcher-kva.example", True),
        ("xn--bcher-kva.example", "bücher.example", True),
        ("*.bücher.example", "a.xn--bcher-kva.example", True),
        ("bücher.example", "bucher.example", False),
        ("BÜCHER.example", "xn--bcher-kva.example", True),
        ("example.com", "ex\u0430mple.com", False),  # a Cyrillic letter: another name
        # IP literals: exact only
        ("93.184.216.34", "93.184.216.34", True),
        ("93.184.216.34", "93.184.216.35", False),
    ],
)
def test_domain_rule_suffix_and_wildcards(pattern: str, host: str, expected: bool) -> None:
    assert _matches(pattern, host) is expected


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "*",
        "*.",
        "*.com",  # a joker over a single label spans a whole TLD
        ".com",
        "a*.example.com",
        "*example.com",
        "foo.*.example.com",
        "*.*.example.com",
        "example..com",
        "-bad.example.com",
        "bad-.example.com",
        "exa mple.com",
        "example.com/path",
        "example.com:443",
        "user@example.com",
        "xn--zz.example",  # invalid punycode
        "a\uff0eexample.com",  # fullwidth full stop: a hidden label separator
        "*.93.184.216.34",
        "93.184.216.*",
        "%65xample.com",
    ],
)
def test_invalid_domain_patterns_are_refused(pattern: str) -> None:
    with pytest.raises(DomainPatternError):
        compile_domain_pattern(pattern)


def test_canonical_host_is_the_a_label_form() -> None:
    assert canonical_host("BÜCHER.Example.") == "xn--bcher-kva.example"
    assert canonical_host("xn--BCHER-KVA.example") == "xn--bcher-kva.example"
    with pytest.raises(DomainPatternError):
        canonical_host("a..b")


def _fetcher(allowlist: DomainAllowlist, rec: Recorder, dns: FakeResolver) -> GuardedFetcher:
    return GuardedFetcher(dns, transport=rec.transport, domains=allowlist)


def test_domain_not_allowed_is_raised_and_logged_with_plugin_id() -> None:
    rules = [DomainRule(pattern="*.example.org", purpose="api")]
    allowlist = DomainAllowlist("plugin-x", rules)
    rec, dns = Recorder(), FakeResolver({"evil.test": [PUBLIC], "a.example.org": [PUBLIC]})
    fetcher = _fetcher(allowlist, rec, dns)
    with structlog.testing.capture_logs() as logs, pytest.raises(DomainNotAllowed) as caught:
        asyncio.run(fetcher.get("https://evil.test/x?token=abc"))
    assert caught.value.code == "source.domain_not_allowed"
    assert rec.requests == [] and dns.calls == []  # refused before any resolution
    (entry,) = [e for e in logs if e["event"] == "plugin.domain_not_allowed"]
    assert entry["plugin_id"] == "plugin-x" and "evil.test" in entry["route"]
    assert "abc" not in str(entry)
    asyncio.run(fetcher.get("https://a.example.org/ok"))
    assert rec.hosts == [PUBLIC]


def test_redirect_off_the_allowlist_is_refused() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.headers["host"] == "a.example.org":
            return httpx2.Response(302, headers={"location": "https://evil.test/next"})
        return httpx2.Response(200, content=b"leak")

    rec = Recorder(respond)
    dns = FakeResolver({"a.example.org": [PUBLIC], "evil.test": [PUBLIC]})
    allowlist = DomainAllowlist("plugin-r", [DomainRule(pattern="a.example.org", purpose="pages")])
    with pytest.raises(DomainNotAllowed):
        asyncio.run(_fetcher(allowlist, rec, dns).get("https://a.example.org/start"))
    assert len(rec.requests) == 1


def test_fetcher_without_allowlist_is_unchanged() -> None:
    rec, dns = Recorder(), FakeResolver({"evil.test": [PUBLIC]})
    asyncio.run(GuardedFetcher(dns, transport=rec.transport).get("https://evil.test/"))
    assert rec.hosts == [PUBLIC]


def _override_rule() -> list[DomainRule]:
    return [
        DomainRule(pattern="api.example.org", purpose="api", base_url_setting="server_url"),
        DomainRule(pattern="*.cdn.example.org", purpose="covers"),
    ]


def test_base_url_override_is_logged() -> None:
    planted = synthetic_text("base-url-override")  # built at run time, registered
    register_secret_value(planted)
    url = f"https://books.example.net/api/{planted}?apikey={planted}"
    with structlog.testing.capture_logs() as logs:
        allowlist = DomainAllowlist("plugin-o", _override_rule(), {"server_url": url})
        DomainAllowlist("plugin-o", _override_rule(), {"server_url": url})  # unchanged: no line
    lines = [e for e in logs if e["event"] == "plugin.base_url_override"]
    assert len(lines) == 1
    (entry,) = lines
    assert entry["plugin_id"] == "plugin-o" and entry["setting"] == "server_url"
    assert entry["log_level"] == "warning"
    assert "books.example.net" in entry["url"] and planted not in str(entry)
    # the override replaces the declared host, it does not add to it
    assert allowlist.allows("books.example.net")
    assert not allowlist.allows("api.example.org")
    assert allowlist.allows("img.cdn.example.org")
    with structlog.testing.capture_logs() as logs:
        DomainAllowlist("plugin-o", _override_rule(), {"server_url": "https://other.example.net"})
        DomainAllowlist("plugin-o", _override_rule(), {})  # back to the declared host
    forget_secret_values()
    changes = [e for e in logs if e["event"] == "plugin.base_url_override"]
    assert [("other.example.net" in e["url"]) for e in changes] == [True, False]
    assert changes[1]["url"] is None


@pytest.mark.parametrize(
    "value",
    [
        "https://user:pw@books.example.net/",
        "ftp://books.example.net/",
        "not a url",
        "https:///nohost",
        42,
    ],
)
def test_bad_base_url_override_fails_closed(value: Any) -> None:
    with structlog.testing.capture_logs() as logs:
        allowlist = DomainAllowlist("plugin-bad", _override_rule(), {"server_url": value})
    assert not allowlist.allows("books.example.net")
    assert not allowlist.allows("api.example.org")  # neither the override nor the default
    (entry,) = [e for e in logs if e["event"] == "plugin.base_url_override_refused"]
    assert entry["plugin_id"] == "plugin-bad" and "pw" not in str(entry)


def test_invalid_rule_in_an_allowlist_fails_closed() -> None:
    with pytest.raises(DomainPatternError):
        DomainAllowlist("plugin-i", [DomainRule(pattern="*.com", purpose="api")])
