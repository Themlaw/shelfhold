"""The Fetcher: the only outbound HTTP path, behind the SSRF guard (ENF-SEC-001, EF-UX-003)."""

from shelfhold.infrastructure.fetcher.client import (
    Download,
    FetchLimits,
    GuardedFetcher,
    IntegrationFetcher,
    Sink,
)
from shelfhold.infrastructure.fetcher.errors import (
    DnsBlocked,
    DnsResolutionError,
    FetchError,
    FetchTimeout,
    FetchTooLarge,
    FetchUnreachable,
    InvalidFetchUrl,
    InvalidLanTarget,
    SsrfBlocked,
    TooManyRedirects,
    UnsafeRedirect,
)
from shelfhold.infrastructure.fetcher.guard import block_reason, is_sinkhole, parse_host
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.fetcher.resolver import Resolver, SystemResolver

__all__ = [
    "DnsBlocked",
    "DnsResolutionError",
    "Download",
    "FetchError",
    "FetchLimits",
    "FetchTimeout",
    "FetchTooLarge",
    "FetchUnreachable",
    "GuardedFetcher",
    "IntegrationFetcher",
    "InvalidFetchUrl",
    "InvalidLanTarget",
    "LanTargets",
    "Resolver",
    "Sink",
    "SsrfBlocked",
    "SystemResolver",
    "TooManyRedirects",
    "UnsafeRedirect",
    "block_reason",
    "is_sinkhole",
    "parse_host",
]
