"""The two addresses the admin gives (D6), checked before a request is made.

The catalogue and the files come from places the admin chose; no default host is written in the
code. What this module refuses is the website itself: a mirror is never `gutenberg.org` or its `www`
host, and an address that holds a credential, is not plain http(s) or names no host is not an
address at all. The check is the plugin's own and comes on top of the host's DomainRules, which
bound the Fetcher by name and by path (ENF-LEG-003).
"""

from __future__ import annotations

import ipaddress
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from shelfhold.sdk.source import NotConfigured

__all__ = [
    "CATALOG_PATH_PREFIX",
    "OFFICIAL_MIRRORS",
    "SETTINGS_KEYS",
    "SiteRefused",
    "epub_url",
    "setting_url",
]

SETTINGS_KEYS = ("catalog_url", "mirror_url")
CATALOG_PATH_PREFIX = "/cache/epub/feeds/"  # where the official feeds are served, on any host
_PROJECT = ("gutenberg.org", "gutenberg.net")  # the project and every host under it
# Owner decision 2026-11-12: the official mirrors of the project that may be the `mirror_url`,
# frozen in the code, never read from a setting, a catalogue or a redirection. Taken from the
# mirror list the project publishes (`gutenberg.org/MIRRORS.ALL`, "Project Gutenberg" providers)
# and from its mirroring how-to (`aleph.gutenberg.org`, which the how-to cites as an rsync point;
# checked 2026-11-12: it also answers plain HTTP, while its HTTPS certificate names
# `aleph.pglaf.org` only, so a verified HTTPS fetch of `aleph.gutenberg.org` fails: use
# `http://`, the file is untrusted and neutralized anyway); none is invented. The website itself
# (`gutenberg.org`, `www.gutenberg.org`) is not on it, and a host under an official name is not
# official: the match is on the whole canonical host name.
OFFICIAL_MIRRORS = frozenset({"aleph.gutenberg.org", "gutenberg.pglaf.org"})
_FEED_HOSTS = frozenset({"gutenberg.org", "www.gutenberg.org"})  # where the feeds are served


class SiteRefused(NotConfigured):
    """The address is the website of the project, not a mirror."""


def _canonical(name: str) -> str:
    """The comparison form of a host: NFKC (full-width letters), A-labels, lower case."""
    text = unicodedata.normalize("NFKC", name).rstrip(".").lower()
    try:
        return text.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise NotConfigured("the address has a host name that is not valid") from error


def _of_project(host: str) -> bool:
    return any(host == p or host.endswith("." + p) for p in _PROJECT)


def _host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = _canonical(parts.hostname or "")
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
    if key == "mirror_url" and host in OFFICIAL_MIRRORS:
        return url  # the shape (scheme, credential, query) was checked by `_host_of` above
    if _of_project(host) and (key == "mirror_url" or host not in _FEED_HOSTS):
        raise SiteRefused("the address is the project's website: a mirror, an official feed")
    if key == "catalog_url" and (not path.startswith(CATALOG_PATH_PREFIX) or path.endswith("/")):
        raise NotConfigured("the catalogue is a file of the official feeds")
    return url


def epub_url(mirror: str, pg_id: int) -> str:
    """Where the mirror keeps the EPUB of a book (`cache/epub/<n>/pg<n>.epub`)."""
    return f"{mirror.rstrip('/')}/cache/epub/{pg_id}/pg{pg_id}.epub"
