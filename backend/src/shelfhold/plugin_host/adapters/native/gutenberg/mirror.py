"""The two addresses the admin gives (D6), checked before a request is made.

The catalogue and the files come from places the admin chose; no default host is written in the
code. What this module refuses is the website itself: a mirror is never `gutenberg.org` or its `www`
host, and an address that holds a credential, is not plain http(s) or names no host is not an
address at all. The check is the plugin's own and comes on top of the host's DomainRules, which
bound the Fetcher by name and by path (ENF-LEG-003).
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from shelfhold.sdk.source import NotConfigured

__all__ = ["CATALOG_PATH_PREFIX", "SETTINGS_KEYS", "SiteRefused", "epub_url", "setting_url"]

SETTINGS_KEYS = ("catalog_url", "mirror_url")
CATALOG_PATH_PREFIX = "/cache/epub/feeds/"  # where the official feeds are served, on any host
_WEBSITE = frozenset({"gutenberg.org", "www.gutenberg.org"})


class SiteRefused(NotConfigured):
    """The address is the website of the project, not a mirror."""


def _host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as error:
        raise NotConfigured("the address is not a URL") from error
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        raise NotConfigured("the address is not a plain http(s) URL without credentials")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise NotConfigured("the address holds a credential, a query or a fragment")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:  # the guard would refuse it, so say so now: a mirror has a name
        raise NotConfigured("the address is an IP number, not a host name")
    return host


def setting_url(settings: dict[str, Any], key: str) -> str:
    """The address given for `key`, or `NotConfigured`. A mirror that is the website is refused;
    so is a catalogue on the website outside the official feeds."""
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NotConfigured(f"{key} is not set")
    url = value.strip()
    host = _host_of(url)
    path = urlsplit(url).path
    if key == "mirror_url" and host in _WEBSITE:
        raise SiteRefused("the mirror is the website: the files come from a mirror")
    if key == "catalog_url" and (not path.startswith(CATALOG_PATH_PREFIX) or path.endswith("/")):
        raise NotConfigured("the catalogue is a file of the official feeds")
    return url


def epub_url(mirror: str, pg_id: int) -> str:
    """Where the mirror keeps the EPUB of a book (`cache/epub/<n>/pg<n>.epub`)."""
    return f"{mirror.rstrip('/')}/cache/epub/{pg_id}/pg{pg_id}.epub"
