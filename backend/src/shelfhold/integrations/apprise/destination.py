"""Check a channel URL when it is configured and again when it is used (ENF-SEC-016, EF-INT-006).

Apprise makes its own requests: they do not go through the Fetcher, so its connection pinning does
not apply to them. What Shelfhold does instead: the scheme is on the allowlist, the URL parses to
exactly one Apprise service, redirects are switched off on that service, and for a self-hosted
service the host is resolved through the Fetcher's resolver and judged by the Fetcher's own rules
(`GuardedFetcher.check_destination`): public addresses only, unless the admin declared the exact
`host:port` in `lan_targets`; the cloud metadata address is never allowed. Limit: Apprise resolves
the name again when it connects, so a DNS answer that changes between the check and the send is
not caught; the check runs at every send to narrow that window, not to close it.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Final

from apprise import Apprise, AppriseAsset, NotifyBase

from shelfhold.infrastructure.fetcher import (
    FetchError,
    IntegrationFetcher,
    LanTargets,
    Resolver,
    parse_host,
)
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.integrations.apprise.schemes import ALLOWED_SCHEMES, HOSTED, SECURE, scheme_of

# Redirects are off for every service built here; a URL's own `redirect=yes` is overridden below.
ASSET: Final = AppriseAsset(http_redirects=False)
_MAX_URL: Final = 2048


class ChannelUrlRefused(ValueError):
    """The URL is not an allowed notification channel. The message never repeats the URL."""


@dataclass(frozen=True)
class ValidatedChannel:
    url: str = field(repr=False)  # carries the token
    scheme: str
    follows_redirects: bool  # always False: kept so that a test can state it


def _parse(url: object) -> tuple[str, NotifyBase]:
    if not isinstance(url, str) or not url or len(url) > _MAX_URL:
        raise ChannelUrlRefused("a channel URL is a non-empty string")
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise ChannelUrlRefused("whitespace and control characters are not allowed")
    scheme = scheme_of(url)
    if scheme is None:
        raise ChannelUrlRefused("the URL has no scheme://")
    if scheme not in ALLOWED_SCHEMES:
        raise ChannelUrlRefused(f"the scheme {scheme!r} is not allowed")
    service = Apprise.instantiate(url, asset=ASSET)
    if service is None:
        raise ChannelUrlRefused(f"the URL is not a valid {scheme!r} channel")
    service.redirects = False  # whatever `?redirect=yes` asked
    if not service.verify_certificate and (scheme not in HOSTED or scheme in SECURE):
        raise ChannelUrlRefused("certificate verification cannot be switched off")
    return scheme, service


def register_url_secrets(url: str) -> None:
    """Scrub every part of the URL that can be a credential from later log lines, by value."""
    register_secret_value(url)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return
    pieces = [parts.password or "", parts.username or ""]
    pieces += [urllib.parse.unquote(s) for s in parts.path.split("/")]
    pieces += [parts.path.strip("/")]
    for name, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        pieces += [name, value]
    for piece in pieces:
        if piece:
            register_secret_value(piece)


async def validate_channel_url(
    url: str, *, lan: LanTargets, resolver: Resolver
) -> ValidatedChannel:
    scheme, service = _parse(url)
    if scheme in HOSTED:
        host = str(getattr(service, "host", None) or "").strip("[]")
        if not host:
            raise ChannelUrlRefused("a self-hosted channel names its server")
        port = int(getattr(service, "port", None) or (443 if scheme in SECURE else 80))
        # Apprise resolves a name itself when it connects, so a public name could point inside by
        # then: a name is allowed only when the admin declared exactly this host:port (lan_targets,
        # whose addresses are re-checked below); anything else must be a literal, non-internal IP.
        try:
            literal = parse_host(host)
        except FetchError as exc:
            raise ChannelUrlRefused(f"the server address is refused: {exc}") from exc
        if literal is None and lan.addresses(host, port) is None:
            raise ChannelUrlRefused(
                "a self-hosted server is an IP address or a declared lan_target"
            )
        checker = IntegrationFetcher(resolver, lan_targets=lan)
        try:
            await checker.check_destination(host, port)
        except FetchError as exc:
            raise ChannelUrlRefused(f"the server is not reachable from here: {exc}") from exc
        finally:
            await checker.aclose()
    register_url_secrets(url)
    return ValidatedChannel(url=url, scheme=scheme, follows_redirects=bool(service.redirects))
