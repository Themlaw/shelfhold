"""The Fetcher: the only outbound HTTP path, behind the SSRF guard (ENF-SEC-001, EF-UX-003)."""

from shelfhold.infrastructure.fetcher.client import FetchLimits, GuardedFetcher
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
)
from shelfhold.infrastructure.fetcher.guard import block_reason, is_sinkhole, parse_host
from shelfhold.infrastructure.fetcher.lan import LanTargets
from shelfhold.infrastructure.fetcher.resolver import Resolver, SystemResolver

__all__ = [
    "DnsBlocked",
    "DnsResolutionError",
    "FetchError",
    "FetchLimits",
    "FetchTimeout",
    "FetchTooLarge",
    "FetchUnreachable",
    "GuardedFetcher",
    "InvalidFetchUrl",
    "InvalidLanTarget",
    "LanTargets",
    "Resolver",
    "SsrfBlocked",
    "SystemResolver",
    "TooManyRedirects",
    "block_reason",
    "is_sinkhole",
    "parse_host",
]
