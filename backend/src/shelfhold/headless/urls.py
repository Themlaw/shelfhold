"""Absolute URLs come from the configured public URL, never from the request (ENF-SEC-016).

A `Host` or `X-Forwarded-*` header is the client's word: a link built from it can be poisoned. The
one source is `SHELFHOLD_PUBLIC_URL` (or `config set public_url`).
"""

import ipaddress
from collections.abc import Callable
from urllib.parse import urlsplit

from shelfhold.infrastructure.fetcher.guard import embedded_v4
from shelfhold.infrastructure.fetcher.resolver import resolve_host

__all__ = [
    "PublicUrlMissing",
    "absolute_url",
    "service_url_problem",
    "validate_public_url",
    "validate_target_url",
]


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


def _lan_name(name: str) -> bool:
    """A name that looks like the household's: single label, `.local`, `.lan`, `.internal`..."""
    return name == "localhost" or "." not in name or name.endswith(_LAN_SUFFIXES)


def _resolver() -> Callable[[str], list[str]]:
    return resolve_host  # looked up at each call: a test replaces it


def carries_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """A global address, or an IPv6 transition form (mapped, NAT64, 6to4, Teredo) that carries a
    global IPv4: reaching it reaches the public network."""
    return address.is_global or any(v4.is_global for v4 in embedded_v4(address))


def validate_target_url(url: str, *, resolve: Callable[[str], list[str]] | None = None) -> str:
    """The rule for `export_target.config["url"]` and every service address (Suwayomi...).

    A public IP literal is refused (a service is a server of the household, reached by name).
    Plain `http://` is for the LAN only: a name must look local (single label, `.lan`...), and it is
    resolved here: `http://` is refused as soon as one of its addresses is global (a name that
    looks local can point anywhere). Any `https://` name is accepted. A local-looking name that
    does not resolve is left to the Fetcher, which pins and checks the addresses at connection.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and carries_public_address(literal):
        raise ValueError("a target is not a public IP address")
    if parts.scheme == "http" and literal is None:
        name = host.lower().rstrip(".")
        if not _lan_name(name):
            raise ValueError("outside the LAN a target must be https://")
        if name != "localhost":
            for found in (resolve or _resolver())(name):
                try:
                    address = ipaddress.ip_address(found)
                except ValueError:
                    continue
                if carries_public_address(address):
                    raise ValueError(
                        f"outside the LAN a target must be https:// ({name} is public)"
                    )
    return url


def service_url_problem(url: str) -> str | None:
    """Why a stored service address is refused now, or None: the rule is applied again each time
    the address is used, not only when it was typed (the file may be edited, a name may move)."""
    try:
        validate_target_url(url)
    except ValueError as error:
        return str(error)
    return None
