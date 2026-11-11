"""Typed Fetcher errors. `code` is the stable catalogue code (EF-UX-003), also the RFC 9457 type."""

from __future__ import annotations

from typing import ClassVar


class FetchError(Exception):
    code: ClassVar[str] = "fetch.error"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class SsrfBlocked(FetchError):
    """The destination is refused by the SSRF guard (an attack or a misconfiguration)."""

    code: ClassVar[str] = "ssrf.blocked"


class DomainNotAllowed(FetchError):
    """The host is not among the DomainRules of the plugin the Fetcher serves (EF-SRC-003)."""

    code: ClassVar[str] = "source.domain_not_allowed"


class DnsBlocked(FetchError):
    """The household resolver answered 0.0.0.0 or :: : a block list, not an attack nor an outage."""

    code: ClassVar[str] = "source.dns_blocked"


class DnsResolutionError(FetchError):
    code: ClassVar[str] = "fetch.dns_failed"


class InvalidFetchUrl(FetchError):
    code: ClassVar[str] = "fetch.invalid_url"


class InvalidLanTarget(ValueError):
    """A lan_targets entry that is not an explicit host:port."""


class TooManyRedirects(FetchError):
    code: ClassVar[str] = "fetch.too_many_redirects"


class UnsafeRedirect(FetchError):
    """A redirection that would carry a request, or its body, to another origin."""

    code: ClassVar[str] = "fetch.unsafe_redirect"


class FetchTooLarge(FetchError):
    code: ClassVar[str] = "fetch.too_large"


class FetchTimeout(FetchError):
    code: ClassVar[str] = "fetch.timeout"


class FetchUnreachable(FetchError):
    code: ClassVar[str] = "fetch.unreachable"


class RobotsDisallowed(FetchError):
    """The site's robots.txt does not allow this path to a native source (ENF-LEG-003)."""

    code: ClassVar[str] = "fetch.robots_disallowed"
