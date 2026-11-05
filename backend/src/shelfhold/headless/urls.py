"""Absolute URLs come from the configured public URL, never from the request (ENF-SEC-016).

A `Host` or `X-Forwarded-*` header is the client's word: a link built from it can be poisoned. The
one source is `SHELFHOLD_PUBLIC_URL` (or `config set public_url`).
"""

import ipaddress
from urllib.parse import urlsplit

__all__ = ["PublicUrlMissing", "absolute_url", "validate_public_url", "validate_target_url"]


class PublicUrlMissing(Exception):
    """No public URL is configured: an absolute link cannot be built."""


def validate_public_url(value: str) -> str:
    """`http(s)://host[:port][/base]`: no credentials, query or fragment; no trailing slash."""
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("a public URL is http(s)://host[:port][/base], nothing else")
    return value.rstrip("/")


def absolute_url(public_url: str | None, path: str) -> str:
    if not public_url:
        raise PublicUrlMissing
    return validate_public_url(public_url) + "/" + path.lstrip("/")


_LAN_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain")


def _lan_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name = host.lower().rstrip(".")
        return name == "localhost" or "." not in name or name.endswith(_LAN_SUFFIXES)
    return not address.is_global


def validate_target_url(url: str) -> str:
    """The rule for `export_target.config["url"]` (shared with every lot that writes it).

    A public IP literal is refused (a destination is a server of the household, reached by name);
    a name or private address outside the LAN forms must be `https://`; plain `http://` is for LAN
    hosts only (private or loopback address, single-label name, `.local`, `.lan`, `.internal`...).
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and literal.is_global:
        raise ValueError("a target is not a public IP address")
    if parts.scheme == "http" and not _lan_host(host):
        raise ValueError("outside the LAN a target must be https://")
    return url
